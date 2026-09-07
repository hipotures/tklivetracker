#!/usr/bin/env bash

set -uo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The three application processes; the supervisor owns recorder subprocesses.
SERVICES=(
    "python scripts/selenium_live_monitor.py"
    "python selenium_supervisor.py --server --debug"
    "python web_monitor/app.py"
)
BACKEND=""
ACTION="start"
action_seen=0
usage() {
    echo "Usage: $0 (--herdr | --tmux) [start|stop|status|restart]"
    echo "Choose exactly one backend: --herdr OR --tmux."
}
for arg in "$@"; do
    case "$arg" in
        --herdr|--tmux)
            if [[ -n "$BACKEND" ]]; then
                usage >&2
                exit 2
            fi
            BACKEND="${arg#--}"
            ;;
        start|stop|status|restart)
            if (( action_seen )); then
                usage >&2
                exit 2
            fi
            ACTION="$arg"
            action_seen=1
            ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
if [[ -z "$BACKEND" ]]; then
    usage >&2
    exit 2
fi
if [[ ! "${STOP_WAIT_SECONDS:-15}" =~ ^[0-9]{1,4}$ ]]; then
    echo "Error: STOP_WAIT_SECONDS must be an integer from 0 to 9999." >&2
    exit 2
fi

WORKSPACE_NAME="TTRACKER"
SESSION_NAME="ttracker"
STOP_WAIT_SECONDS="$((10#${STOP_WAIT_SECONDS:-15}))"

# Keep the original lock name for compatibility with the previous launcher.
LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/ttracker-herdr.lock"

# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------

requirements=(uv flock ps)
[[ "$BACKEND" == herdr ]] && requirements+=(herdr jq) || requirements+=(tmux)
for cmd in "${requirements[@]}"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Error: required command '$cmd' not found."
        exit 1
    fi
done


# Prevent two copies of this control script from modifying the same tabs
# simultaneously.
exec 9>"$LOCK_FILE" || exit 1

if ! flock -n 9; then
    echo "Error: another instance of this script is already running."
    exit 1
fi

# Terminal servers must not inherit the launcher's lock descriptor.
tmux() { command tmux "$@" 9>&-; }
herdr() { command herdr "$@" 9>&-; }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

get_tab_name() {
    local cmd="$1"

    if [[ "$cmd" == *"selenium_live_monitor.py"* ]]; then
        echo "monitor"
    elif [[ "$cmd" == *"selenium_supervisor.py"* ]]; then
        echo "supervisor"
    elif [[ "$cmd" == *"web_monitor/app.py"* ]]; then
        echo "web"
    else
        echo "cmd_$(printf '%s' "$cmd" | cksum | cut -d' ' -f1)"
    fi
}

get_workspace_id() {
    if [[ "$BACKEND" == tmux ]]; then
        printf '%s\n' "$SESSION_NAME"
        return
    fi
    local response
    local ids=()

    response="$(herdr workspace list 2>/dev/null)" || return 1

    mapfile -t ids < <(
        jq -r --arg name "$WORKSPACE_NAME" '
            .result.workspaces[]?
            | select(.label == $name)
            | .workspace_id
        ' <<<"$response"
    )

    if [[ "${#ids[@]}" -eq 0 ]]; then
        echo "Error: Herdr workspace '$WORKSPACE_NAME' not found." >&2
        return 1
    fi

    if [[ "${#ids[@]}" -gt 1 ]]; then
        echo "Error: multiple Herdr workspaces named '$WORKSPACE_NAME' found." >&2
        return 1
    fi

    printf '%s\n' "${ids[0]}"
}

get_tab_id() {
    local workspace_id="$1"
    local tab_name="$2"

    if [[ "$BACKEND" == tmux ]]; then
        if ! tmux has-session -t "=$workspace_id" 2>/dev/null; then
            return 0
        fi
        local windows id name found=""
        windows="$(tmux list-windows -t "=$workspace_id" -F '#{window_id} #{window_name}')" || return 1
        while read -r id name; do
            [[ "$name" == "$tab_name" ]] || continue
            [[ -z "$found" ]] || { echo "Error: duplicate window '$tab_name'." >&2; return 1; }
            found="$id"
        done <<<"$windows"
        printf '%s\n' "$found"
        return
    fi

    herdr tab list --workspace "$workspace_id" 2>/dev/null |
        jq -r --arg name "$tab_name" '
            .result.tabs[]?
            | select(.label == $name)
            | .tab_id
        ' |
        head -n1
}

get_pane_id() {
    local workspace_id="$1"
    local tab_id="$2"
    if [[ "$BACKEND" == tmux ]]; then
        local panes
        panes="$(tmux list-panes -t "$tab_id" -F '#{pane_id}')" || return 1
        [[ -n "$panes" && "$panes" != *$'\n'* ]] || {
            echo "Error: expected exactly one pane in $tab_id." >&2
            return 1
        }
        printf '%s\n' "$panes"
        return
    fi
    herdr pane list --workspace "$workspace_id" 2>/dev/null |
        jq -r --arg tab_id "$tab_id" '
            .result.panes[]?
            | select(.tab_id == $tab_id)
            | .pane_id
        ' | head -n1
}

is_shell_name() {
    local name
    name="$(basename "$1")"

    case "$name" in
        bash|zsh|fish|sh|dash|ksh|mksh|ash|nu|pwsh|powershell)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

has_non_shell_descendant() {
    local parent_pid="$1"
    local child_pid
    local child_name

    while read -r child_pid child_name; do
        [[ -z "${child_pid:-}" ]] && continue

        if ! is_shell_name "$child_name"; then
            return 0
        fi

        if has_non_shell_descendant "$child_pid"; then
            return 0
        fi
    done < <(
        ps -o pid=,comm= --ppid "$parent_pid" 2>/dev/null
    )

    return 1
}

get_pane_state() {
    local pane_id="$1"
    local response
    local shell_pid
    local names=()
    local name

    if [[ "$BACKEND" == tmux ]]; then
        local dead current
        response="$(tmux display-message -p -t "$pane_id" '#{pane_dead} #{pane_current_command} #{pane_pid}')" || { echo UNKNOWN; return; }
        read -r dead current shell_pid <<<"$response"
        if [[ "$dead" == 1 ]]; then
            echo IDLE
        elif [[ "$dead" == 0 && -n "$current" ]]; then
            # tmux services run directly, without an interactive shell.
            echo RUNNING
        else
            echo UNKNOWN
        fi
        return
    fi

    response="$(
        herdr pane process-info --pane "$pane_id" 2>/dev/null
    )"

    if [[ -z "$response" ]]; then
        echo "UNKNOWN"
        return
    fi

    if ! jq -e '.error == null and (.result.process_info | type == "object")' >/dev/null 2>&1 <<<"$response"; then
        echo "UNKNOWN"
        return
    fi

    mapfile -t names < <(
        jq -r '
            .result.process_info.foreground_processes[]?
            | (.name // .argv0 // "")
            | select(length > 0)
        ' <<<"$response"
    )

    # If Herdr reports foreground processes, consider the pane idle only
    # when every foreground process is a shell.
    if [[ "${#names[@]}" -gt 0 ]]; then
        for name in "${names[@]}"; do
            if ! is_shell_name "$name"; then
                echo "RUNNING"
                return
            fi
        done

        echo "IDLE"
        return
    fi

    # Fallback: inspect descendants of the pane shell.
    shell_pid="$(
        jq -r '.result.process_info.shell_pid // empty' <<<"$response"
    )"

    if [[ -n "$shell_pid" && "$shell_pid" =~ ^[0-9]+$ ]]; then
        if has_non_shell_descendant "$shell_pid"; then
            echo "RUNNING"
        else
            echo "IDLE"
        fi
        return
    fi

    echo "UNKNOWN"
}

create_service_tab() {
    local workspace_id="$1"
    local tab_name="$2"
    local response
    local pane_id

    if [[ "$BACKEND" == tmux ]]; then
        # Create an empty retained pane before starting the service, so even
        # an immediate startup failure remains visible and restartable.
        if tmux has-session -t "=$workspace_id" 2>/dev/null; then
            pane_id="$(tmux new-window -d -P -F '#{pane_id}' -t "$workspace_id:" -n "$tab_name" -c "$PROJECT_DIR" '')" || return 1
        else
            pane_id="$(tmux new-session -d -P -F '#{pane_id}' -s "$workspace_id" -n "$tab_name" -c "$PROJECT_DIR" '')" || return 1
        fi
        tmux set-option -w -t "$pane_id" remain-on-exit on >/dev/null || return 1
        tmux set-option -w -t "$pane_id" automatic-rename off >/dev/null || return 1
        printf '%s\n' "$pane_id"
        return
    fi

    response="$(
        herdr tab create \
            --workspace "$workspace_id" \
            --cwd "$PROJECT_DIR" \
            --label "$tab_name" \
            --no-focus
    )"

    pane_id="$(
        jq -r '.result.root_pane.pane_id // empty' <<<"$response"
    )"

    if [[ -z "$pane_id" ]]; then
        echo "Error: failed to create tab '$tab_name'." >&2
        echo "$response" >&2
        return 1
    fi

    printf '%s\n' "$pane_id"
}

run_service() {
    local pane_id="$1"
    local line="$2"
    local quoted_project_dir

    printf -v quoted_project_dir '%q' "$PROJECT_DIR"

    if [[ "$BACKEND" == tmux ]]; then
        tmux respawn-pane -t "$pane_id" -c "$PROJECT_DIR" "exec uv run $line"
        return
    fi

    herdr pane run \
        "$pane_id" \
        "cd $quoted_project_dir && uv run $line"
}

# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

start_services() {
    local workspace_id
    local line
    local tab_name
    local tab_id
    local pane_id
    local state

    workspace_id="$(get_workspace_id)" || exit 1

    echo "Starting ttracker services using $BACKEND..."
    echo

    for line in "${SERVICES[@]}"; do
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

        tab_name="$(get_tab_name "$line")"
        tab_id="$(get_tab_id "$workspace_id" "$tab_name")" || return 1

        if [[ -z "$tab_id" ]]; then
            echo "[$tab_name] Tab missing -> creating it."

            pane_id="$(
                create_service_tab "$workspace_id" "$tab_name"
            )" || return 1

            echo "[$tab_name] Starting: uv run $line"
            run_service "$pane_id" "$line" || return 1
            continue
        fi

        pane_id="$(get_pane_id "$workspace_id" "$tab_id")" || return 1

        if [[ -z "$pane_id" ]]; then
            echo "[$tab_name] Error: tab exists but no pane was found."
            continue
        fi

        state="$(get_pane_state "$pane_id")"

        case "$state" in
            IDLE)
                echo "[$tab_name] Tab exists and is idle -> starting."
                run_service "$pane_id" "$line" || return 1
                ;;

            RUNNING)
                echo "[$tab_name] Already running -> skipping."
                ;;

            *)
                echo "[$tab_name] Cannot determine pane state -> skipping for safety."
                return 1
                ;;
        esac
    done
    if [[ "$BACKEND" == tmux ]]; then
        echo "Attach: tmux attach -t $SESSION_NAME"
        echo "Switch windows: Ctrl+b then n/p; detach: Ctrl+b then d."
    fi
}

send_interrupt() {
    if [[ "$BACKEND" == tmux ]]; then
        tmux send-keys -t "$1" C-c
    else
        herdr pane send-keys "$1" ctrl+c
    fi
}

# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

stop_services() {
    local workspace_id
    local line
    local tab_name
    local tab_id
    local pane_id
    local state

    workspace_id="$(get_workspace_id)" || exit 1

    echo "Stopping ttracker services..."
    echo

    for line in "${SERVICES[@]}"; do
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

        tab_name="$(get_tab_name "$line")"
        tab_id="$(get_tab_id "$workspace_id" "$tab_name")" || return 1

        if [[ -z "$tab_id" ]]; then
            echo "[$tab_name] Tab does not exist."
            continue
        fi

        pane_id="$(get_pane_id "$workspace_id" "$tab_id")" || return 1

        if [[ -z "$pane_id" ]]; then
            echo "[$tab_name] No pane found."
            continue
        fi

        state="$(get_pane_state "$pane_id")"

        case "$state" in
            IDLE)
                echo "[$tab_name] Already stopped."
                ;;

            RUNNING)
                echo "[$tab_name] Sending Ctrl-C..."
                send_interrupt "$pane_id" || return 1
                ;;

            UNKNOWN)
                echo "[$tab_name] State unknown -> skipping for safety." >&2
                return 1
                ;;
        esac
    done

    echo
    echo "Tabs were left open."
}

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

status_services() {
    local workspace_id
    local line
    local tab_name
    local tab_id
    local pane_id
    local state

    workspace_id="$(get_workspace_id)" || exit 1

    printf "%-14s %-10s %s\n" "SERVICE" "STATE" "TAB"
    printf "%-14s %-10s %s\n" "--------------" "----------" "----------------"

    for line in "${SERVICES[@]}"; do
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

        tab_name="$(get_tab_name "$line")"
        tab_id="$(get_tab_id "$workspace_id" "$tab_name")" || return 1

        if [[ -z "$tab_id" ]]; then
            printf "%-14s %-10s %s\n" "$tab_name" "MISSING" "-"
            continue
        fi

        pane_id="$(get_pane_id "$workspace_id" "$tab_id")" || return 1

        if [[ -z "$pane_id" ]]; then
            printf "%-14s %-10s %s\n" "$tab_name" "UNKNOWN" "$tab_id"
            continue
        fi

        state="$(get_pane_state "$pane_id")"

        printf "%-14s %-10s %s\n" "$tab_name" "$state" "$tab_id"
    done
}

# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

all_services_stopped() {
    local workspace_id="$1"
    local line
    local tab_name
    local tab_id
    local pane_id
    local state

    for line in "${SERVICES[@]}"; do
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

        tab_name="$(get_tab_name "$line")"
        tab_id="$(get_tab_id "$workspace_id" "$tab_name")" || return 1

        [[ -z "$tab_id" ]] && continue

        pane_id="$(get_pane_id "$workspace_id" "$tab_id")" || return 1
        [[ -z "$pane_id" ]] && continue

        state="$(get_pane_state "$pane_id")"

        if [[ "$state" != "IDLE" ]]; then
            return 1
        fi
    done

    return 0
}

restart_services() {
    local workspace_id
    local elapsed=0

    workspace_id="$(get_workspace_id)" || exit 1

    stop_services || return 1

    echo
    echo "Waiting for services to stop..."

    while (( elapsed <= STOP_WAIT_SECONDS )); do
        if all_services_stopped "$workspace_id"; then
            echo "All services stopped."
            echo
            start_services
            return
        fi

        (( elapsed == STOP_WAIT_SECONDS )) && break
        sleep 1
        ((elapsed++))
    done

    echo
    echo "Timeout after ${STOP_WAIT_SECONDS}s."
    echo "Running services will NOT be started a second time."
    echo

    return 1
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

case "$ACTION" in
    start)
        start_services
        ;;

    stop)
        stop_services
        ;;

    status)
        status_services
        ;;

    restart)
        restart_services
        ;;

    *)
        echo "Usage: $0 [start|stop|status|restart]"
        exit 2
        ;;
esac
\
