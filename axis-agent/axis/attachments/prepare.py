"""Download/cache the small CPU embedding model and check local inference.

From axis-agent: uv run python -m axis.attachments.prepare
No document contents are sent to the model download service.
"""
import argparse
import json
from pathlib import Path
import time

from .search import Embeddings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parents[2] / '.axis-ui')
    args = parser.parse_args()
    encoder = Embeddings(args.data_dir / 'attachments' / 'model-cache')
    started = time.monotonic()
    documents = encoder.encode(['Terminate inactive sessions after 30 minutes.', 'Choose a blue theme for the dashboard.'])
    query = encoder.encode(['automatic logout timeout'], query=True)[0]
    scores = documents @ query
    if scores[0] <= scores[1]:
        raise RuntimeError('The semantic retrieval smoke check failed.')
    print(json.dumps(dict(model='BAAI/bge-small-en-v1.5', dimensions=len(query), semantic_check='passed', seconds=round(time.monotonic() - started, 2))))


if __name__ == '__main__':
    main()
