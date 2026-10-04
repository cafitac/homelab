#!/usr/bin/env bash
# k8s 노드 VM 을 띄운다 — Mac Studio 에서 실행한다. 여러 번 실행해도 된다.
#   bash ~/Project/homelab/cluster/up.sh
#
# ⭐ VM 데이터 디스크(_lima/_disks/colima-k8s/datadisk)는 외장 SSD 에 둔다 → 모든 PV(local-path)가 SSD 에 생긴다.
#    lima 는 디스크 폴더가 미리 있으면 만들기를 거부한다(10/5 확인). 그래서 처음 한 번은 내장 디스크에 만들고,
#    멈춘 뒤 SSD 로 복사하고 링크로 바꾼다 — thread-example infra/storage.sh 와 같은 방식.
# ⭐ --activate=false — colima 가 전역 docker / kube 컨텍스트를 바꾸지 않게 한다(10/5 thread-example 재시작 때 바뀌었다).
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH

PROFILE=k8s
SSD_ROOT=/Volumes/TradingData/k8s
DST=$SSD_ROOT/colima-disks/colima-$PROFILE
SRC=$HOME/.colima/_lima/_disks/colima-$PROFILE

# 최상위 폴더는 처음 한 번 사람이 만든다(외장 SSD 루트는 root 소유):
#   sudo mkdir -p /Volumes/TradingData/k8s && sudo chown cafitac:staff /Volumes/TradingData/k8s
mount | grep -q " on /Volumes/TradingData " || { echo "외장 SSD 가 마운트되지 않았다" >&2; exit 1; }
[ -w "$SSD_ROOT" ] || { echo "$SSD_ROOT 에 쓸 수 없다 — 위 주석대로 폴더를 만든다" >&2; exit 1; }
# 링크가 끊긴 채 켜면 colima 가 빈 디스크를 새로 만들어 PV 가 사라진 것처럼 보인다
if [ -L "$SRC" ] && [ ! -e "$SRC" ]; then
  echo "데이터 디스크 링크가 끊겼다 → $(readlink "$SRC")" >&2; exit 1
fi

start() {
  # --k3s-arg 를 주면 기본값(--disable=traefik)이 대체된다 → Traefik 이 켜진다
  # --mount none — 호스트 폴더를 VM 에 열지 않는다. 데이터는 전부 PV 로
  colima start -p "$PROFILE" --activate=false \
    --vm-type vz --runtime docker \
    --cpu 16 --memory 64 --disk 1000 \
    --mount none \
    --dns 1.1.1.1 --dns 8.8.8.8 \
    --kubernetes --k3s-arg=--write-kubeconfig-mode=0644
}

relocate() {
  colima stop -p "$PROFILE"
  mkdir -p "$(dirname "$DST")"
  rm -rf "$DST.partial"
  # ⭐ cp 는 APFS 에서 sparse 파일의 빈 구간을 유지한다 — 겉보기 500GB 를 실제 쓴 만큼만 복사한다
  cp -Rp "$SRC" "$DST.partial"
  [ "$(stat -f %z "$SRC/datadisk")" = "$(stat -f %z "$DST.partial/datadisk")" ] || { echo "복사본 크기가 다르다" >&2; exit 1; }
  mv "$DST.partial" "$DST"
  # 원본은 SSD 에서 뜨는 것을 확인한 뒤 사람이 지운다: rm -rf "$SRC.moved"
  mv "$SRC" "$SRC.moved"
  ln -s "$DST" "$SRC"
  echo "데이터 디스크를 SSD 로 옮겼다 → $DST"
}

# ⚠️ k3s API 포트는 처음 설치할 때 무작위로 정해져 k3s 서비스 파일에 남는다(지금 55902). 재시작해도 그대로다.
#    --k3s-listen-port 를 나중에 바꿔도 이미 설치된 k3s 에는 적용되지 않는다(10/5 확인) — 원격 kubectl 은 실제 포트를 읽어 쓴다
# 코어 수를 바꾸면(12 → 16, 2026-10-05 judge-board 공식 채점 몫) 한 번 내렸다 올린다
# 디스크를 늘리면(500 → 1000GB, 2026-10-05 thread-example DB) 같다
if colima status -p "$PROFILE" >/dev/null 2>&1 && { ! grep -qE '^cpu: 16$' "$HOME/.colima/$PROFILE/colima.yaml" \
    || ! grep -qE '^disk: 1000$' "$HOME/.colima/$PROFILE/colima.yaml"; }; then
  colima stop -p "$PROFILE"
fi
if ! colima status -p "$PROFILE" >/dev/null 2>&1; then
  start
fi
if [ ! -L "$SRC" ]; then
  relocate
  start
fi

# ⭐ kubelet static CPU manager — Guaranteed 파드의 정수 코어를 독점시킨다(측정 · 채점이 이웃 부하에 흔들리지 않게).
#    코어 0-1 은 시스템 · k3s 몫으로 남긴다. ⚠️ k3s 인자(--k3s-arg)는 처음 설치 때만 들어가므로 설정 파일로 둔다.
#    정책을 바꾸면 kubelet 의 CPU 상태 파일을 지우고 다시 띄워야 한다(그대로 두면 kubelet 이 시작을 거부한다)
K3S_CONFIG='kubelet-arg:
  - cpu-manager-policy=static
  - reserved-cpus=0-1'
if [ "$(colima ssh -p "$PROFILE" -- sudo cat /etc/rancher/k3s/config.yaml 2>/dev/null)" != "$K3S_CONFIG" ]; then
  printf '%s\n' "$K3S_CONFIG" | colima ssh -p "$PROFILE" -- sudo tee /etc/rancher/k3s/config.yaml >/dev/null
  colima ssh -p "$PROFILE" -- sh -c 'sudo systemctl stop k3s && sudo rm -f /var/lib/kubelet/cpu_manager_state && sudo systemctl start k3s'
  echo "k3s: static CPU manager 적용"
fi

# kubeconfig — 전역 ~/.kube/config 를 건드리지 않고 따로 둔다. KUBECONFIG=~/.kube/homelab.yaml 로 쓴다
mkdir -p "$HOME/.kube"
colima ssh -p "$PROFILE" -- sudo cat /etc/rancher/k3s/k3s.yaml \
  | sed -e 's/: default$/: homelab/' > "$HOME/.kube/homelab.yaml.tmp"
mv "$HOME/.kube/homelab.yaml.tmp" "$HOME/.kube/homelab.yaml"
chmod 600 "$HOME/.kube/homelab.yaml"
echo "k8s VM 준비됨 — export KUBECONFIG=~/.kube/homelab.yaml"
