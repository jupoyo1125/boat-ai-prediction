# BOAT AI Ver.6 公開手順

1. このフォルダの中身をGitHubの新しいリポジトリへアップロード。
2. RenderでNew -> Web Serviceを選択し、GitHubリポジトリを接続。
3. Runtime: Python 3
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `gunicorn app:app`
6. Deployを実行。
7. 発行された `onrender.com` URLをiPhoneのSafariで開く。

注意:
- 収支・学習データは現在JSONファイル保存。ホスティング環境の再デプロイ等で永続性が保証されない場合があります。
- 本番運用ではPostgreSQL等の永続DBへの移行を推奨。
