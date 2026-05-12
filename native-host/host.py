import sys
import json
import struct
import subprocess
import os


def read_message():
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length or len(raw_length) < 4:
        return None
    length = struct.unpack('I', raw_length)[0]
    message = sys.stdin.buffer.read(length).decode('utf-8')
    return json.loads(message)


def send_message(message):
    encoded = json.dumps(message).encode('utf-8')
    sys.stdout.buffer.write(struct.pack('I', len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _project_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _port_file_path():
    return os.path.join(_project_dir(), 'data', 'api_port.txt')


def _read_port_file():
    try:
        with open(_port_file_path(), 'r') as f:
            return int(f.read().strip())
    except Exception:
        return None


def _write_port_file(port):
    try:
        os.makedirs(os.path.join(_project_dir(), 'data'), exist_ok=True)
        with open(_port_file_path(), 'w') as f:
            f.write(str(port))
    except Exception:
        pass


def main():
    msg = read_message()
    if not msg:
        return

    action = msg.get('action')

    if action == 'get_port':
        port = _read_port_file()
        if port:
            send_message({'ok': True, 'port': port})
        else:
            send_message({'ok': False, 'error': 'No port file found'})

    elif action == 'start_server':
        project_dir = _project_dir()
        venv_python = os.path.join(project_dir, '.venv', 'Scripts', 'python.exe')
        python_exe = venv_python if os.path.exists(venv_python) else 'python'
        port = msg.get('port', 0)
        try:
            cmd = [python_exe, 'run.py', 'api', '--port', str(port)]
            if port == 0:
                cmd = [python_exe, 'run.py', 'api']
            subprocess.Popen(
                cmd,
                cwd=project_dir,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                stdout=open(os.path.join(project_dir, 'data', 'server.log'), 'w'),
                stderr=subprocess.STDOUT,
            )
            send_message({'ok': True, 'message': 'Server starting...'})
        except Exception as e:
            send_message({'ok': False, 'error': str(e)})

    else:
        send_message({'ok': False, 'error': 'Unknown action'})


if __name__ == '__main__':
    main()
