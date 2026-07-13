if (
    __import__("os").environ.get("MDP_SIM_MODE") is not None
    or "--sim" in __import__("sys").argv
):
    from mdp_controller.__sim_bus import MDPBus
    from mdp_controller.__sim_mdp_l1060 import MDP_L1060
    from mdp_controller.__sim_mdp_p906 import MDP_P906
else:
    from mdp_controller.bus import MDPBus
    from mdp_controller.mdp_l1060 import MDP_L1060
    from mdp_controller.mdp_p906 import MDP_P906

__all__ = ["MDP_P906", "MDP_L1060", "MDPBus"]
