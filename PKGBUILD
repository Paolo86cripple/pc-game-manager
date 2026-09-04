pkgname=pc-game-manager
pkgver=2.12
pkgrel=1
pkgdesc='Sandboxed PC game manager and launcher for Arch Linux'
arch=('x86_64')
license=('GPL-3.0-only')
depends=('python' 'python-gobject' 'gtk4' 'bubblewrap' 'wine' 'winetricks' 'lspci' 'pipewire-pulse')
optdepends=('umu-run: Proton/UMU runtime support' 'dosbox-staging: DOS and Windows 3.x games' 'cdemu-client: optical image integration')
source=()
sha256sums=()
package() {
  local root="$srcdir/pc-game-manager-2.12"
  install -Dm755 "$root/bin/pc-game-sandbox" "$pkgdir/usr/bin/pc-game-sandbox"
  install -Dm755 "$root/bin/pc-game-manager" "$pkgdir/usr/bin/pc-game-manager"
  install -Dm755 "$root/pc-game-manager.py" "$pkgdir/usr/share/pc-game-manager/pc-game-manager.py"
  install -Dm644 "$root/lib/runners.py" "$pkgdir/usr/share/pc-game-manager/lib/runners.py"
  install -Dm644 "$root/lib/runtime_manager.py" "$pkgdir/usr/share/pc-game-manager/lib/runtime_manager.py"
  install -Dm644 "$root/lib/renderer_manager.py" "$pkgdir/usr/share/pc-game-manager/lib/renderer_manager.py"
  install -Dm644 "$root/profiles/default.json" "$pkgdir/usr/share/pc-game-manager/profiles/default.json"
  install -Dm644 "$root/pc-game-manager.desktop" "$pkgdir/usr/share/applications/pc-game-manager.desktop"
  install -Dm644 "$root/README.md" "$pkgdir/usr/share/doc/pc-game-manager/README.md"
  install -Dm644 "$root/docs/security.md" "$pkgdir/usr/share/doc/pc-game-manager/security.md"
  install -Dm644 "$root/docs/sandbox-review.md" "$pkgdir/usr/share/doc/pc-game-manager/sandbox-review.md"
  install -Dm644 "$root/docs/architecture.md" "$pkgdir/usr/share/doc/pc-game-manager/architecture.md"
}
