"""Local process stop requests do not acknowledge remote cancellation."""
import os
import subprocess
import tempfile
import time


def stop_local(process):
    state = {'cancellation_requested': False, 'local_process_exited': process.poll() is not None,
             'local_tree_termination': 'unknown', 'remote_execution': 'unknown'}
    if state['local_process_exited']:
        return state
    state['cancellation_requested'] = True
    try:
        if os.name == 'nt':
            # PID comes only from our Popen object; never from user/provider text.
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           capture_output=True, timeout=5)
        else:
            process.kill()
        process.wait(timeout=5)
        state['local_process_exited'] = True
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=5)
            state['local_process_exited'] = True
        except (OSError, subprocess.TimeoutExpired):
            pass
    # A request or a successful taskkill exit alone does not establish parent exit.
    state['local_process_exited'] = process.poll() is not None
    return state


def run_bounded(command, prompt, cwd, env, timeout, should_stop=None):
    """should_stop: polled about once a second so another process can cancel through the ledger."""
    if not 0 < timeout <= 3600:
        raise ValueError('timeout must be within one hour')
    # Files avoid pipe reader threads/locks and descendant-held pipes during failed cleanup.
    # The prompt still enters through stdin; raw diagnostics are discarded.
    with tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile() as outgoing:
        incoming.write(prompt.encode('utf-8'))
        incoming.seek(0)
        p = subprocess.Popen(command, cwd=cwd, env=env, stdin=incoming,
            stdout=outgoing, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        while True:
            try:
                p.wait(timeout=max(0.01, min(1, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                operator = bool(should_stop and should_stop())
                if operator or time.monotonic() >= deadline:
                    return '', p.returncode, {**stop_local(p), 'reason': 'operator' if operator else 'timeout'}
        outgoing.seek(0)
        # Bound normalization input independently of provider output size.
        out = outgoing.read(1024 * 1024 + 1)
        if len(out) > 1024 * 1024:
            return '', p.returncode, None
        return out.decode('utf-8', errors='replace'), p.returncode, None
