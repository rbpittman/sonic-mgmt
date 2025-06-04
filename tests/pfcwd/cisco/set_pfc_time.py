from common import tree, d0, dd0, port_to_sai_lane_map, sai_lane_to_slice_ifg_pif, \
    is_gb, is_pac, is_gr, is_graphene2
import math
import sdk

# Input parameters replaced via 'sed'
param_interface = __PARAM_INTERFACE__  # noqa: F821
param_success = __PARAM_SUCCESS__      # noqa: F821


def get_ifg_reg_list(slice_idx):
    ''' Gr2 does not have an ifg list, listify '''
    if is_graphene2:
        ifg_root = [tree.slice[slice_idx].ifg]
    else:
        ifg_root = tree.slice[slice_idx].ifg
    return ifg_root


def get_ifgb(ifg_root):
    ''' Complex tree register differences for ifgb per asic.
            Takes tree.slice[slice_idx].ifg[ifg_idx] '''
    if is_graphene2:
        ifgb = ifg_root.ifgbe_ra
    elif is_gr:
        ifgb = ifg_root.ifgbe_mac
    else:
        ifgb = ifg_root.ifgb
    return ifgb


def set_pfc_512bit_time(interface, bit_time, num_serdes_lanes):
    sai_lane = port_to_sai_lane_map[interface]
    slice_idx, ifg_idx, serdes_idx = sai_lane_to_slice_ifg_pif(sai_lane)
    for i in range(num_serdes_lanes):
        ifg_root = get_ifg_reg_list(slice_idx)[ifg_idx]
        ifg_mac = get_ifgb(ifg_root)
        regval = dd0.read_register(ifg_mac.fc_port_cfg0[serdes_idx + i])
        regval.port_512bit_time = bit_time
        dd0.write_register(ifg_mac.fc_port_cfg0[serdes_idx + i], regval)


def set_pfc512_bit_sec(interface, time_sec):
    if is_gb or is_pac:
        khz = d0.get_int_property(sdk.la_device_property_e_DEVICE_FREQUENCY)
        print("Device frequency khz: {}".format(khz))
    elif is_gr or is_graphene2:
        khz = d0.get_int_property(sdk.la_device_property_e_MAC_FREQUENCY)
        print("Mac frequency khz: {}".format(khz))
    else:
        assert False, "Unsupported device type"
    clock_time = 1. / (khz * 1000)
    num_clocks_float = time_sec / (65535 * clock_time)

    if is_gb or is_pac:
        bit_time = math.ceil(num_clocks_float)
    elif is_gr or is_graphene2:
        int_part = int(num_clocks_float)
        float_part = num_clocks_float - int_part
        print("Integer: {}".format(int_part))
        print("Float: {}".format(float_part))
        bit_time = (int_part << 10) + int(float_part * 1024)
        if bit_time >= 2 ** 18:
            print("Maxed out, setting bit time {} instead of {}".format((2 ** 18) - 1, bit_time))
            bit_time = (2 ** 18) - 1
    else:
        assert False, "Unsupported device type"
    print("Setting bit_time (number of clocks) to {}".format(bit_time))
    set_pfc_512bit_time(interface, bit_time, num_serdes_lanes=1)


if __name__ == "__main__":
    # Increase PFC pause time
    num_ms = 50
    print("Setting PFC frame time to {}ms".format(num_ms))
    set_pfc512_bit_sec(param_interface, num_ms / 1000)
    print(param_success)
