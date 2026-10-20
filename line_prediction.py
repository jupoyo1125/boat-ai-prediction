"""Prepare a dated LINE-only selection from the same pre-race model inputs."""
from datetime import datetime
import os
import re

from race_venues import STADIUMS

FIXED_FIRST_RULE = 'fixed-first-v1'
FIXED_PARTNERS_RULE = 'fixed-first-partners-v1'


def condition_for_race(date, stadium, race):
    value = os.getenv('BOAT_LINE_FIXED_FIRST', '').strip()
    if not value:
        return None
    match = re.fullmatch(
        r'(\d{8}):(\d{2}):(0?[1-9]|1[0-2]):([1-6])(?::([1-6](?:,[1-6])*))?', value)
    if match is None:
        raise ValueError('invalid dated LINE condition')
    selected_date, selected_stadium, selected_race, first, raw_partners = match.groups()
    datetime.strptime(selected_date, '%Y%m%d')
    if selected_stadium not in STADIUMS:
        raise ValueError('invalid LINE venue')
    first = int(first)
    partners = sorted(int(value) for value in raw_partners.split(',')) if raw_partners else []
    if len(set(partners)) != len(partners) or first in partners:
        raise ValueError('invalid LINE partners')
    if (date, stadium, race) != (selected_date, selected_stadium, int(selected_race)):
        return None
    condition = {'rule': FIXED_PARTNERS_RULE if partners else FIXED_FIRST_RULE,
                 'first': first}
    if partners:
        condition['partners'] = partners
    return condition


def fixed_first_for_race(date, stadium, race):
    condition = condition_for_race(date, stadium, race)
    return condition['first'] if condition is not None else None


def prepare_forecast(date, stadium, race, boats, odds, scenario, model, build_bets):
    # An invalid LINE-only setting must not stop the site's normal forecasts.
    # Delivery validates it again and cannot fall back to ordinary buy lists.
    try:
        condition = condition_for_race(date, stadium, race)
    except ValueError:
        return None
    if condition is None:
        return None
    if condition.get('partners'):
        # Filter the candidate odds before category selection. The model still
        # evaluates all six boats, so probabilities are not renormalized.
        partners = {str(boat) for boat in condition['partners']}
        odds = {combo: value for combo, value in odds.items()
                if partners.intersection(combo[1:])}
    return {**condition,
            'bets': build_bets(boats, odds, str(condition['first']), scenario, model=model,
                               exclude_gachi_from_roman=True)}
