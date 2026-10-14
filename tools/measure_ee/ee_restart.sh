#!/usr/bin/env bash
# Sourced by the teardown scripts: restart_ee LOG starts EasyEffects in
# service mode and returns only once its pipeline links to a sink.
#
# A running EE with easyeffects_sink listed is not restored audio: restarted
# amid graph churn, EE can come up with no processing graph behind its sink,
# which then swallows every app's audio. Only links out of its last stage
# prove the pipeline is back, so poll for them and restart once more if they
# never appear. Returns non-zero if the second start fails too.
#
# The check only means something right after a start. EE links its filters at
# startup, then unlinks them once no app has played for inactivityTimeout
# (default 10 s), so an idle graph with no EE links is normal.

ee_linked() {
    # grep -c reads to EOF: an early -q exit could SIGPIPE pw-link under
    # pipefail and read as no links.
    pw-link --links | grep -c '<- ee_soe_output_level:output_' >/dev/null
}

wait_for_ee_links() {
    for _ in $(seq 20); do
        ee_linked && return 0
        sleep 0.5
    done
    return 1
}

start_ee() {
    nohup easyeffects --hide-window --service-mode >"$1" 2>&1 &
    disown
    echo "restarted easyeffects (service mode)"
}

restart_ee() {
    start_ee "$1"
    if ! wait_for_ee_links; then
        echo "easyeffects came up without output links; restarting it once more"
        easyeffects -q || true
        sleep 2
        start_ee "$1"
        if ! wait_for_ee_links; then
            echo "ERROR: easyeffects still has no output links, so apps routed" \
                 "to it are silent. Restart it by hand: easyeffects -q, then" \
                 "easyeffects --hide-window --service-mode" >&2
            return 1
        fi
    fi
    echo
    echo "--- easyeffects output links ---"
    pw-link --links | grep -B1 '<- ee_soe_output_level:output_'
}
