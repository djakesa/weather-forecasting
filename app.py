"""Start the local API and dedicated web frontend: python app.py."""
import argparse
import json
import socket
from urllib.request import ProxyHandler, build_opener


def main():
    parser = argparse.ArgumentParser(description="Atlas solaire local")
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Le port doit être compris entre 1 et 65535.')
    url = f'http://127.0.0.1:{args.port}'
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(('127.0.0.1', args.port))
        except OSError as exc:
            if exc.errno not in {48, 98, 10048} and getattr(exc, 'winerror', None) != 10048:
                raise
            try:
                with build_opener(ProxyHandler({})).open(url + '/api/health', timeout=2) as response:
                    existing = json.load(response).get('service') == 'atlas-solaire'
            except (OSError, ValueError, AttributeError):
                existing = False
            if existing:
                print(f'Atlas solaire est déjà lancé. Ouvrez {url}')
                return 0
            print(f'Le port {args.port} est occupé par un autre service. '
                  f'Relancez avec : python app.py --port {8001 if args.port != 8001 else 8002}')
            return 1
        import uvicorn
        listener.listen(128)
        print(f'Atlas solaire : {url}', flush=True)
        uvicorn.Server(uvicorn.Config('backend.main:app', host='127.0.0.1', port=args.port)).run(sockets=[listener])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
