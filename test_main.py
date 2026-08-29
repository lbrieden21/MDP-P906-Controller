import time

from mdp_controller import MDP_P906, MDPBus

if __name__ == "__main__":
    # Address and channel come from MDPBus's defaults; the P906 is on pipe 1.
    bus = MDPBus(tx_output_power="4dBm", debug=False)
    mdp = MDP_P906(bus, idcode="FE2597EC", led_color=(0x66, 0xCC, 0xFF), debug=False)

    try:
        bus.attach(mdp, 1)
        mdp.connect()
    except Exception:
        print("Connection failed, try to auto match")
        idcode, pipe = bus.auto_match()
        mdp = MDP_P906(bus, idcode=idcode, led_color=(0x66, 0xCC, 0xFF), debug=False)
        bus.attach(mdp, pipe)
        mdp.connect()

    mdp.set_voltage(5)
    mdp.set_current(2)
    mdp.set_output(True)
    print(mdp.get_set_voltage_current())
    print(mdp.get_status())
    print(mdp.get_realtime_value())

    t0 = time.perf_counter()
    cnt = 0
    cnt2 = 0

    def rt_cbk(vals):
        global cnt, t0, cnt2
        cnt += 1
        t1 = time.perf_counter() - t0
        print(
            f"{t1/cnt:0.5f}s {cnt/t1:5.1f}fps {t1/cnt2:0.5f}s {cnt2/t1:5.1f}fps {vals}             \r",
            end="",
        )

    # async
    # mdp.register_realtime_value_callback(rt_cbk)
    # while True:
    #     cnt2 += 1
    #     vals = mdp.request_realtime_value()

    # sync
    while True:
        cnt2 += 1
        vals = mdp.get_status()
        # vals = mdp.get_realtime_value()
        rt_cbk(vals)
