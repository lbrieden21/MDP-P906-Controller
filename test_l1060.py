import sys
import time

sys.argv.append("--sim")

from mdp_controller import MDP_L1060, MDPBus  # noqa: E402

if __name__ == "__main__":
    bus = MDPBus(tx_output_power="4dBm", debug=False)
    idcode, pipe = bus.auto_match()
    mdp = MDP_L1060(bus, idcode=idcode, led_color=(0x66, 0xCC, 0xFF), debug=False)
    bus.attach(mdp, pipe)
    mdp.connect()

    mdp.select_mode("CC")
    mdp.set_current(1.5)
    print("set_load_on(True):", mdp.set_load_on(True))
    print("get_status():", mdp.get_status())
    print("get_realtime_value():", mdp.get_realtime_value())

    mdp.request_target_page()
    time.sleep(0.05)
    print("get_targets():", mdp.get_targets())

    mdp.select_mode("CV")
    mdp.set_voltage(5.0)
    print("get_status() CV:", mdp.get_status())

    mdp.select_mode("CR")
    mdp.set_resistance(50.0)
    print("get_status() CR:", mdp.get_status())

    mdp.select_mode("CP")
    mdp.set_power(10.0)
    print("get_status() CP:", mdp.get_status())

    print("set_load_on(False):", mdp.set_load_on(False))
    mdp.set_led_color((0xFF, 0x00, 0x00))

    # Trip the sim's fake OCP latch and confirm the no-RF-clear behavior.
    mdp.select_mode("CC")
    mdp.set_current(15.0)
    ok = mdp.set_load_on(True)
    print("set_load_on(True) while over-threshold CC:", ok)
    print("get_status() after latch:", mdp.get_status())
    print("set_load_on(True) retry while latched:", mdp.set_load_on(True))

    cnt = [0]

    def rt_cbk(vals):
        cnt[0] += 1
        if cnt[0] <= 3:
            print(f"realtime callback #{cnt[0]}: {vals}")

    mdp.register_realtime_value_callback(rt_cbk)
    for _ in range(5):
        mdp.request_realtime_value()
    print(f"realtime callback fired {cnt[0]} times")

    mdp.close()
    bus.close()
    print("OK")
