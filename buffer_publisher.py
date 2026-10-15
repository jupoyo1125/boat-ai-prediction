"""The Buffer GraphQL adapter; credentials remain on the server.

Create a private draft first. Publishing that existing draft is the only
operation that can make the forecast visible, and is never blindly retried.
"""
from urllib.parse import urlsplit

import requests

from threads_publisher import ThreadsError, WrongAccount, USERNAME

API = 'https://api.buffer.com'
POST_FIELDS = '''id status text sentAt externalLink schedulingType
    channel { id name service }
    assets { id }
    metadata { ... on ThreadsPostMetadata { threadCount } }'''


class BufferClient:
    def __init__(self, token, channel_id, session=None):
        self.session = session or requests.Session()
        self.channel_id = channel_id
        self._headers = {'Authorization': f'Bearer {token}'}

    def request(self, query, variables):
        response = self.session.request(
            'POST', API, headers=self._headers, json={'query': query, 'variables': variables},
            timeout=15, allow_redirects=False)
        if not 200 <= response.status_code < 300:
            raise ThreadsError(f'Buffer API HTTP {response.status_code}')
        payload = response.json()
        # API errors can contain user data. Do not persist or log their text.
        if (not isinstance(payload, dict) or payload.get('errors')
                or not isinstance(payload.get('data'), dict)):
            raise ThreadsError('Buffer API response invalid')
        return payload['data']

    def check_channel(self, channel):
        if (not isinstance(channel, dict) or channel.get('id') != self.channel_id
                or channel.get('service') != 'threads'
                or str(channel.get('name', '')).removeprefix('@').lower() != USERNAME):
            raise WrongAccount('Bufferの投稿先が指定Threadsアカウントと一致しません。')

    def verify_account(self):
        result = self.request('''query ForecastChannel($id: ChannelId!) {
            channel(input: {id: $id}) {
                id name service isDisconnected isLocked isQueuePaused
            }
        }''', {'id': self.channel_id})
        channel = result.get('channel')
        self.check_channel(channel)
        if any(channel.get(k) is not False
               for k in ('isDisconnected', 'isLocked', 'isQueuePaused')):
            raise ThreadsError('Buffer channel unavailable')

    def post_result(self, value, text=None):
        if not isinstance(value, dict):
            raise ThreadsError('Buffer post missing')
        self.check_channel(value.get('channel'))
        post_id = value.get('id')
        if (not isinstance(post_id, str) or not post_id or len(post_id) > 256
                or value.get('status') not in ('draft', 'scheduled', 'sending', 'sent',
                                              'error', 'needs_approval')
                or value.get('schedulingType') != 'automatic'
                or (text is not None and value.get('text') != text)
                or value.get('assets') != []
                or (value.get('metadata') or {}).get('threadCount', 0) != 0):
            raise ThreadsError('Buffer post does not match the saved forecast')
        return value

    def action(self, operation, input_type, values, text=None):
        data = self.request(
            f'''mutation ForecastAction($input: {input_type}!) {{
                {operation}(input: $input) {{
                    ... on PostActionSuccess {{ post {{ {POST_FIELDS} }} }}
                    ... on MutationError {{ __typename }}
                }}
            }}''', {'input': values})
        return self.post_result((data.get(operation) or {}).get('post'), text)

    def create(self, text):
        post = self.action('createPost', 'CreatePostInput', {
            'channelId': self.channel_id, 'text': text, 'assets': [],
            'needsApproval': False, 'saveToDraft': True,
            'schedulingType': 'automatic', 'mode': 'addToQueue',
            'metadata': {'threads': {'type': 'post', 'thread': []}},
        }, text)
        if post['status'] != 'draft':
            raise WrongAccount('Buffer did not return a private draft')
        return post['id']

    def publication(self, post_id, text=None):
        data = self.request(f'''query ForecastPost($id: PostId!) {{
            post(input: {{id: $id}}) {{ {POST_FIELDS} }}
        }}''', {'id': post_id})
        post = self.post_result(data.get('post'), text)
        if post['id'] != post_id:
            raise ThreadsError('Buffer post id mismatch')
        return post

    def validate_draft(self, post_id, text):
        if self.publication(post_id, text)['status'] != 'draft':
            # A draft changed outside this worker must never be published again.
            raise WrongAccount('Bufferの下書きが投稿準備時の状態と一致しません。')

    def publish(self, post_id, text):
        post = self.action('editPost', 'EditPostInput', {
            'id': post_id, 'text': text, 'assets': [], 'saveToDraft': False,
            'schedulingType': 'automatic', 'mode': 'shareNow',
            'metadata': {'threads': {'type': 'post', 'thread': []}},
        }, text)
        if post['status'] not in ('scheduled', 'sending', 'sent'):
            raise ThreadsError('Buffer publication not accepted')
        return post['id']

    def withdraw(self, post_id):
        # This preserves the Buffer post as a draft; it never deletes a native post.
        post = self.action('editPost', 'EditPostInput', {
            'id': post_id, 'saveToDraft': True,
        })
        if post['status'] != 'draft':
            raise ThreadsError('Buffer withdrawal not confirmed')

    @staticmethod
    def permalink(post):
        link = post.get('externalLink') or ''
        parsed = urlsplit(link)
        if (parsed.scheme == 'https' and parsed.hostname in
                ('threads.com', 'www.threads.com', 'threads.net', 'www.threads.net')
                and parsed.path.startswith(f'/@{USERNAME}/post/')
                and not parsed.username and not parsed.password):
            return link
        return ''
