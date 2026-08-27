import json
import re
from tests.common.reboot import reboot
from tests.common.utilities import wait_until


# =============================================================================
# Cisco 8000 Constants
# =============================================================================
CISCO_ASIC_TYPE = "cisco-8000"

# Platform prefixes for conditional_mark and other test infrastructure.
CISCO_8122_PREFIX = "x86_64-8122"        # Matches both GR2 (x86_64-8122_*) and GR2X (x86_64-8122x*)
CISCO_8122_GR2_PREFIX = "x86_64-8122_"   # GR2 only (note trailing underscore)
CISCO_8122_GR2X_PREFIX = "x86_64-8122x"  # GR2X only (note 'x' suffix)
CISCO_8223_PREFIX = "x86_64-8223_"       # Matches 8223 platforms (x86_64-8223_*)

# Legacy aliases (kept for backward compatibility)
GR2X_PLATFORM_PREFIX = CISCO_8122_GR2X_PREFIX
GR2X_HWSKU_PREFIX = "Cisco-8122X"


def is_cisco_device(dut):
    return dut.facts["asic_type"] == CISCO_ASIC_TYPE


def is_model_json_format(duthost):
    model_json_platforms = ['x86_64-8102_64h_o-r0']
    return duthost.facts['platform'] in model_json_platforms


def get_markings_config_file(duthost):
    """
        Get the config file where the ECN markings are enabled or disabled.
    """
    if duthost.facts["asic_type"] != CISCO_ASIC_TYPE:
        raise RuntimeError("This is applicable only to cisco platforms.")
    platform = duthost.facts['platform']
    hwsku = duthost.facts['hwsku']
    if is_model_json_format(duthost):
        match = re.search(r"\-([^-_]+)_", platform)
        if match:
            model = match.group(1)
        else:
            raise RuntimeError("Couldn't get the model from platform:{}".format(platform))
    else:
        model = "serdes"
    config_file = "/usr/share/sonic/device/{}/{}/{}.json".format(platform, hwsku, model)
    return config_file


def get_markings_dut(duthost, key_list=['ecn_dequeue_marking', 'ecn_latency_marking', 'voq_allocation_mode']):
    """
        Get the ecn marking values from the duthost.
    """
    config_file = get_markings_config_file(duthost)
    dest_file = "/tmp/"
    contents = duthost.fetch(src=config_file, dest=dest_file)
    local_file = contents['dest']
    with open(local_file) as fd:
        json_contents = json.load(fd)
    markings_dict = {}
    # Getting markings from first device.
    device = json_contents['devices'][0]
    for key in key_list:
        markings_dict[key] = device['device_property'][key]
    return markings_dict


def setup_markings_dut(duthost, localhost, **kwargs):
    """
        Setup dequeue or latency depending on arguments.
        Applicable to cisco-8000 Platforms only.
    """
    config_file = get_markings_config_file(duthost)
    dest_file = "/tmp/"
    contents = duthost.fetch(src=config_file, dest=dest_file)
    local_file = contents['dest']
    with open(local_file) as fd:
        json_contents = json.load(fd)
    reboot_required = False
    for device in json_contents['devices']:
        for k, v in list(kwargs.items()):
            if device['device_property'][k] != v:
                reboot_required = True
                device['device_property'][k] = v
    if reboot_required:
        duthost.copy(content=json.dumps(json_contents, sort_keys=True, indent=4), dest=config_file)
        reboot(duthost, localhost)


def copy_dshell_script_cisco_8000(dut, asic, dshell_script, script_name):
    if dut.facts['asic_type'] != CISCO_ASIC_TYPE:
        raise RuntimeError("This function should have been called only for cisco-8000.")

    script_path = "/tmp/{}".format(script_name)
    dut.copy(content=dshell_script, dest=script_path)
    if dut.sonichost.is_multi_asic:
        dest = f"syncd{asic}"
    else:
        dest = "syncd"
    dut.shell(f"docker cp {script_path} {dest}:/")  # noqa: E231


def copy_set_voq_watchdog_script_cisco_8000(dut, asic="", enable=True):
    dshell_script = '''
from common import d0
def set_voq_watchdog(enable):
    d0.set_bool_property(sdk.la_device_property_e_VOQ_WATCHDOG_ENABLED, enable)
set_voq_watchdog({})
'''.format(enable)

    copy_dshell_script_cisco_8000(dut, asic, dshell_script, script_name="set_voq_watchdog.py")


def check_dshell_ready(duthost):
    show_command = "sudo show platform npu rx cgm_global"
    err_msg = "debug shell server for asic 0 is not running"
    output = duthost.command(show_command)['stdout']
    if err_msg in output:
        return False
    return True


def run_dshell_command(duthost, command):
    if not wait_until(300, 20, 0, check_dshell_ready, duthost):
        raise RuntimeError("Debug shell is not ready on {}".format(duthost.hostname))
    return duthost.shell(command)


# =============================================================================
# HBM buffer packing parameters
# =============================================================================
# Read the HBM packing parameters from "show platform npu global" and compute
# the expected HBM buffer pool watermark for a given packet load. Retrieval
# (needs a duthost) is kept separate from the pure formula helpers so the math
# can be reused/unit-tested without a live device.
#
# Packing model: each packet costs (packet_size + header_size) bytes and packs
# into an effective region of (hbm_burst_size * HBM_BURST_UNIT_BYTES) bytes,
# capped at max_pds_in_pack packets, then flushes to one hbm_buffer_size buffer.

# The burst-size register is expressed in fixed-size units the CLI does not report.
HBM_BURST_UNIT_BYTES = 512

# SMS occupancy at which a single lossless queue evicts from SMS to HBM on Cisco P200.
HBM_SINGLE_QUEUE_EVICT_P200_BYTES = 44 * 1024 * 1024

_SIZE_UNIT_MULTIPLIERS = {
    "B": 1,
    "BYTE": 1,
    "BYTES": 1,
    "KB": 1024,
    "MB": 1024 ** 2,
    "GB": 1024 ** 3,
}

# Fields at the top of "show platform npu global"; whitespace before the colon
# is inconsistent, so \s* is used liberally. The command labels these fields
# "DRAM ...", so the match strings keep that wording.
_HBM_SIZE_FIELD_PATTERNS = {
    # Reported with a size unit (e.g. "8 KB").
    "hbm_buffer_size_bytes": r"DRAM buffer size\s*:\s*(\d+)\s*([A-Za-z]+)?",
    "sms_memory_size_bytes": r"SMS Memory Size\s*:\s*(\d+)\s*([A-Za-z]+)?",
}
_HBM_COUNT_FIELD_PATTERNS = {
    # Unitless integer fields.
    "hbm_burst_size": r"DRAM burst size\s*:\s*(\d+)",
    "max_pds_in_pack": r"DRAM max packet descriptors in a pack\s*:\s*(\d+)",
    "header_size_bytes": r"DRAM header size per packet descriptor\s*:\s*(\d+)",
}
# Fields required to compute the HBM watermark.
_REQUIRED_HBM_FIELDS = (
    "hbm_buffer_size_bytes",
    "hbm_burst_size",
    "max_pds_in_pack",
    "header_size_bytes",
)


def _size_to_bytes(number, unit):
    """Convert a "<number> <unit>" size (e.g. 8, "KB") to an integer byte count."""
    number = int(number)
    if not unit:
        return number
    multiplier = _SIZE_UNIT_MULTIPLIERS.get(unit.upper())
    if multiplier is None:
        raise ValueError(
            "Unrecognized size unit '{}' in 'show platform npu global' output".format(unit))
    return number * multiplier


def parse_hbm_parameters(output):
    """
    Parse HBM/SMS parameters from "show platform npu global" output.

    Raises ValueError if a required field is missing, so a CLI format change fails
    loudly instead of silently producing wrong watermark expectations.
    """
    params = {}
    for key, pattern in _HBM_SIZE_FIELD_PATTERNS.items():
        match = re.search(pattern, output)
        if match:
            params[key] = _size_to_bytes(match.group(1), match.group(2))
    for key, pattern in _HBM_COUNT_FIELD_PATTERNS.items():
        match = re.search(pattern, output)
        if match:
            params[key] = int(match.group(1))

    missing = [key for key in _REQUIRED_HBM_FIELDS if key not in params]
    if missing:
        raise ValueError(
            "Could not parse HBM parameters {} from 'show platform npu global'. "
            "Raw output:\n{}".format(missing, output))
    return params


def get_hbm_parameters(duthost, asic_index=None):
    """
    Retrieve live HBM packing parameters from a Cisco 8000 DUT.

    asic_index adds a "-n asic<index>" namespace option for multi-asic platforms.
    """
    namespace_option = ""
    if asic_index is not None:
        namespace_option = " -n asic{}".format(asic_index)
    show_command = "show platform npu global{}".format(namespace_option)
    result = run_dshell_command(duthost, show_command)
    return parse_hbm_parameters(result["stdout"])


def hbm_pkts_per_buffer(packet_size, hbm_params, burst_unit_bytes=HBM_BURST_UNIT_BYTES):
    """Packets that pack into a single HBM buffer for the given packet size."""
    effective_pack_bytes = hbm_params["hbm_burst_size"] * burst_unit_bytes
    per_pkt_bytes = packet_size + hbm_params["header_size_bytes"]
    return min(hbm_params["max_pds_in_pack"], effective_pack_bytes // per_pkt_bytes)


def hbm_bytes_per_packet(packet_size, hbm_params, burst_unit_bytes=HBM_BURST_UNIT_BYTES):
    """Effective HBM occupancy (bytes) per packet once evicted to HBM."""
    pkts_per_buffer = hbm_pkts_per_buffer(packet_size, hbm_params, burst_unit_bytes)
    if pkts_per_buffer <= 0:
        raise ValueError("Packet size {} does not fit the HBM pack path".format(packet_size))
    return hbm_params["hbm_buffer_size_bytes"] / pkts_per_buffer


def expected_hbm_watermark_bytes(num_packets, packet_size, hbm_params,
                                 burst_unit_bytes=HBM_BURST_UNIT_BYTES):
    """
    Expected HBM buffer pool watermark after num_packets have evicted to HBM.

    Rounds up to whole buffers, since a partially-filled pack still consumes one.
    """
    pkts_per_buffer = hbm_pkts_per_buffer(packet_size, hbm_params, burst_unit_bytes)
    if pkts_per_buffer <= 0:
        raise ValueError("Packet size {} does not fit the HBM pack path".format(packet_size))
    num_buffers = (num_packets + pkts_per_buffer - 1) // pkts_per_buffer
    return num_buffers * hbm_params["hbm_buffer_size_bytes"]
