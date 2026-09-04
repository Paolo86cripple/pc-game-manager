from pathlib import Path
import tarfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import renderer_manager as rm


def _make_archive(path: Path, kind: str):
    tree = path.parent / 'tree'
    tree.mkdir(exist_ok=True)
    if kind == 'dxvk':
        (tree / 'x64').mkdir(exist_ok=True)
        (tree / 'x32').mkdir(exist_ok=True)
        for d in ('x64', 'x32'):
            (tree / d / 'd3d11.dll').write_bytes(b'dll')
            (tree / d / 'dxgi.dll').write_bytes(b'dll')
    else:
        (tree / 'x32').mkdir(exist_ok=True)
        (tree / 'x32' / 'ddraw.dll').write_bytes(b'dll')
    with tarfile.open(path, 'w:gz') as tf:
        tf.add(tree, arcname=f'{kind}-test')


def test_discover_managed_renderers(tmp_path):
    dx = tmp_path / 'dxvk' / 'v1'; (dx / 'x64').mkdir(parents=True)
    (dx / 'x64' / 'd3d11.dll').write_bytes(b'x'); (dx / 'x64' / 'dxgi.dll').write_bytes(b'x')
    d7 = tmp_path / 'd7vk' / 'v2'; (d7 / 'x32').mkdir(parents=True)
    (d7 / 'x32' / 'ddraw.dll').write_bytes(b'x')
    found = rm.discover(tmp_path)
    assert {x['kind'] for x in found} == {'dxvk', 'd7vk'}


def test_install_dxvk_and_d7vk(tmp_path, monkeypatch):
    archives = {}
    for kind in ('dxvk', 'd7vk'):
        arc = tmp_path / f'{kind}.tar.gz'; _make_archive(arc, kind); archives[kind] = arc
    def fake_download(url, target):
        kind = 'd7vk' if 'd7vk' in url else 'dxvk'
        target.write_bytes(archives[kind].read_bytes())
    monkeypatch.setattr(rm, '_download', fake_download)
    for kind in ('dxvk', 'd7vk'):
        item = {'kind': kind, 'url': f'https://example/{kind}.tar.gz', 'filename': f'{kind}.tar.gz', 'version': 'vtest'}
        p = rm.install(item, tmp_path / 'managed')
        assert p.is_dir()
        assert any(x.suffix.lower() == '.dll' for x in p.rglob('*'))
