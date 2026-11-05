# Docker 빌드 캐시 주간 정리 (Mac Studio)

colima `k8s` 프로필의 BuildKit 빌드 캐시가 한 번 349GB 까지 쌓였다. 두 겹으로 막는다.

1. **주간 정리** — `cluster/docker-cache-gc.sh` 가 매주 일요일 04:30 에 `docker builder prune --filter until=168h` 와 `fstrim` 을 돌린다. 이미지 · 볼륨 · 컨테이너는 건드리지 않는다.
2. **상한** — BuildKit 자체 GC 상한을 colima 설정에 넣는다. **colima 를 다시 시작해야 적용된다.**

스크립트는 colima k8s 가 꺼져 있거나 `colima-k8s` 컨텍스트가 `~/.colima/k8s/docker.sock` 을 가리키지 않으면 아무것도 지우지 않고 끝낸다. Discord 알림은 Alertmanager 가 쓰는 Secret(`monitoring/alertmanager-discord`)을 `kubectl` 로 읽어 보낸다 — k3s 가 colima 안에 있어서 colima 가 꺼져 있으면 알림도 못 가고 기록(`~/Library/Logs/docker-cache-gc.log`)만 남는다.

## 설치 (Mac Studio, 한 번)

```bash
cd ~/Project/homelab && git pull --ff-only
cp ~/Project/homelab/cluster/launchd/com.cafitac.docker-cache-gc.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cafitac.docker-cache-gc.plist
```

한 번 직접 돌려 본다(첫 실행은 오래 걸릴 수 있다).

```bash
bash ~/Project/homelab/cluster/docker-cache-gc.sh
```

기록은 `~/Library/Logs/docker-cache-gc.log` — 전 · 후 `/Volumes/TradingData` 사용량과 회수한 바이트가 남는다. launchd 로 뜬 bash 는 `/Volumes` 에 쓸 수 없으므로(TCC) 스크립트는 거기에 쓰지 않는다.

## 영구 상한 (BuildKit GC)

`~/.colima/k8s/colima.yaml` 의 `docker:` 아래에 `builder` 를 추가한다. Docker 29.5.2 (`docker --context colima-k8s version`)에서 `builder.gc` 는 데몬 설정 `builder: {gc: {enabled, defaultKeepStorage}}` 와 같은 모양이다.

```bash
cp ~/.colima/k8s/colima.yaml ~/.colima/k8s/colima.yaml.bak-$(date +%Y%m%d)
```

```yaml
docker:
  registry-mirrors:
    - http://127.0.0.1:5055
  log-driver: json-file
  log-opts:
    max-file: "3"
    max-size: 100m
  builder:
    gc:
      enabled: true
      defaultKeepStorage: "150GB"
```

⚠️ 이 설정은 **colima k8s 를 다음에 재시작할 때** 적용된다(`colima stop -p k8s && colima start -p k8s`). 재시작하는 동안 이 VM 의 서비스가 잠깐 끊기므로 **소유자가 때를 골라 직접** 한다. 재시작하기 전까지는 주간 정리만 동작한다.
