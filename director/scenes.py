"""Scene selection — STUB (Task 2 scaffold). Task 3 replaces this with the real
salience-driven policy: severity-tier intent, a mutation-checked minimum dwell,
and decay back to the calm home scene when the world is quiet."""


def choose_scene(state, dir_state, now, config):
    return dir_state.current_scene
