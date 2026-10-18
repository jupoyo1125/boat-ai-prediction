"""Prepare a dated LINE-only selection from the same pre-race model inputs."""
from datetime import datetime
import os
import re

from race_venues import STADIUMS

FIXED_FIRST_RULE = 'fixed-first-v1'


def fixed_first_for_race(date, stadium, race):
    value = os.getenv('BOAT_LINE_FIXED_FIRST', '').strip()
    if not value:
        return None
    match = re.fullmatch(r'(\d{8}):(\d{2}):(0?[1-9]|1[0-2]):([1-6])', value)
    if match is None:
        raise ValueError('invalid dated LINE condition')
    selected_date, selected_stadium, selected_race, first = match.groups()
    datetime.strptime(selected_date, '%Y%m%d')
    if selected_stadium not in STADIUMS:
        raise ValueError('invalid LINE venue')
    if (date, stadium, race) != (selected_date, selected_stadium, int(selected_race)):
        return None
    return int(first)


def prepare_forecast(date, stadium, race, boats, odds, scenario, model, build_bets):
    # An invalid LINE-only setting must not stop the site's normal forecasts.
    # Delivery validates it again and cannot fall back to ordinary buy lists.
    try:
        first = fixed_first_for_race(date, stadium, race)
    except ValueError:
        return None
    if first is None:
        return None
    return {'rule': FIXED_FIRST_RULE, 'first': first,
            'bets': build_bets(boats, odds, str(first), scenario, model=model,
                               exclude_gachi_from_roman=True)}
