"""Local process stop requests do not acknowledge remote cancellation."""
import os
import subprocess


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
        process.kill()
        try:
            process.wait(timeout=5)
            state['local_process_exited'] = True
        except subprocess.TimeoutExpired:
            pass
    return state


def run_bounded(command, prompt, cwd, env, timeout):
    if not 0 < timeout <= 60:
        raise ValueError('timeout must be within 60 seconds')
    p = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    try:
        out, _ = p.communicate(prompt, timeout=timeout)
        return out, p.returncode, None
    except subprocess.TimeoutExpired:
        cancellation = stop_local(p)
        # Close pipes; do not wait indefinitely on descendants holding a pipe open.
        for stream in (p.stdin, p.stdout, p.stderr):
            if stream:
                stream.close()
        return '', p.returncode, cancellation
