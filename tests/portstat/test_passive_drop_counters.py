"""Verify that port RX/TX drop counters do not increase passively.

On a healthy, idle DUT the per-port drop counters (RX_DRP / TX_DRP) should be
stable. If they keep climbing while no test traffic is being sent, it usually
points to a misconfiguration or a background problem (unexpected control
traffic being dropped, a flapping link, etc.). This test snapshots the drop
counters, waits, and then asserts that none of them have increased.
"""
import logging

import pytest

from tests.common.helpers.assertions import pytest_assert
from tests.common.portstat_utilities import parse_portstat
from tests.common.utilities import wait

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('any')
]

# How long to observe the counters for passive increases.
MONITOR_PERIOD_SECONDS = 60

# Portstat columns that represent dropped packets.
DROP_FIELDS = ('rx_drp', 'tx_drp')


def _counter_to_int(value):
    """Convert a portstat counter cell to an int, treating N/A as 0."""
    if value is None:
        return 0
    value = value.replace(',', '').strip()
    if value in ('', 'N/A'):
        return 0
    return int(value)


def _get_drop_counters(duthost):
    """Return {intf: {'rx_drp': int, 'tx_drp': int}} for every interface."""
    portstat = parse_portstat(duthost.command('portstat')['stdout_lines'])
    pytest_assert(portstat, 'Failed to parse portstat output on {}'.format(duthost.hostname))
    return {
        intf: {field: _counter_to_int(fields.get(field)) for field in DROP_FIELDS}
        for intf, fields in portstat.items()
    }


def test_no_passive_drop_counter_increase(duthosts, enum_rand_one_per_hwsku_frontend_hostname):
    """Verify RX/TX drop counters do not increase while the DUT is idle."""
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    baseline = _get_drop_counters(duthost)
    logger.info('Captured baseline drop counters for %d interfaces', len(baseline))

    wait(MONITOR_PERIOD_SECONDS, 'Observe whether drop counters increase passively')

    updated = _get_drop_counters(duthost)

    increased = []
    for intf, base_fields in baseline.items():
        if intf not in updated:
            logger.warning('Interface %s disappeared from portstat output; skipping', intf)
            continue
        for field in DROP_FIELDS:
            delta = updated[intf][field] - base_fields[field]
            if delta > 0:
                increased.append((intf, field, base_fields[field], updated[intf][field], delta))
                logger.error('%s %s increased by %d (%d -> %d)',
                             intf, field.upper(), delta, base_fields[field], updated[intf][field])

    pytest_assert(
        not increased,
        'Drop counters increased passively over {}s on: {}'.format(
            MONITOR_PERIOD_SECONDS,
            ['{} {} (+{})'.format(intf, field.upper(), delta)
             for intf, field, _, _, delta in increased]))
