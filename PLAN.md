# homelab — Mac Studio 서비스를 k8s 한 클러스터로 옮기는 계획

- 작성: 2026-10-05
- 상태: 0 단계 진행 중
- ⭐ 최종 목표 (사용자, 2026-10-05): **Mac Studio 의 모든 사이드 프로젝트를 k8s 로 전환한다.** colima 는 k8s 노드 VM 하나만 남긴다
- 대상 호스트: Mac Studio `trading-macstudio` (M-시리즈 16코어 · 128GB · 외장 SSD `/Volumes/TradingData` 1.8TB, 여유 1.5TB)

## 1. 왜 옮기나

colima 프로필(= VM)을 프로젝트마다 하나씩 두는 지금 구조에서 실제로 겪은 문제다.

| 겪은 일 | 원인 (colima 구조) | k8s 에서는 |
| --- | --- | --- |
| 10/3 thread-example DB 가 디스크 부족으로 33 시간 다운 | VM 마다 디스크 크기 고정(40GB). SSD 는 1.5TB 가 비어 있었다 | PVC 단위 용량 · 전체는 SSD 하나를 공유 |
| VM 재시작 뒤 DB 컨테이너가 네트워크에서 빠져 앱이 `postgres` 를 못 찾음 | 컨테이너 상태를 사람이 Terraform apply 로 되돌려야 함 | Deployment · Service 가 선언 상태로 스스로 복구 |
| 실행 중 VM 만 33 코어 · 73GB 를 미리 잡고 있음(실사용은 훨씬 적다) | 자원을 VM 단위로 떼어 줌 | namespace 의 ResourceQuota · LimitRange, 남는 자원은 공유 |
| Cloudflare 터널이 프로젝트마다 하나(5 개) | 프로젝트별 compose 에 cloudflared 가 따로 | 터널 1 개 + Ingress 가 호스트명으로 분기 |
| `colima start` 가 전역 도커 컨텍스트를 바꿈 · 배포 스크립트마다 컨텍스트 고정 | 관리 지점이 VM 수만큼 흩어짐 | `kubectl` 하나, 매니페스트 저장소 하나 |
| preview-hub 디스크가 SSD 가 아니라 본체에 있음 | 프로필마다 디스크 위치를 손으로 옮겨야 함 | 클러스터 VM 디스크 하나만 SSD 에 두면 모든 PV 가 SSD |

## 2. 지켜야 할 규칙 (사용자 결정)

- 모든 서비스 데이터는 외장 SSD 에 둔다.
- 프로젝트마다 자원을 명확히 분리한다 → namespace + ResourceQuota + LimitRange + NetworkPolicy.
- 경보는 Mac Studio 전체 범위로 만들되, 받는 창구는 사용자가 정할 때까지 연결하지 않는다.
- puri 는 사용자 프로젝트가 아니다 — 소유자와 상의 없이 옮기지 않는다.
- earlypay-tests 는 회사 테스트용 — 대상이 아니다.

## 3. 목표 구조

```text
Mac Studio (macOS)
└─ colima 프로필 k8s  (vz · virtiofs · 디스크 = 외장 SSD 심볼릭 링크)
   └─ k3s 단일 노드
      ├─ platform        cloudflared(터널 1개) · Traefik · local-path(SSD) · metrics
      ├─ monitoring      kube-prometheus-stack (경보 규칙만, 수신처 미연결)
      ├─ portfolio       nginx
      ├─ interview-coach web · server · knowledge · embedder · postgres(pgvector) · 백업 CronJob
      ├─ judge-board     board · frontend · postgres · runner(+ 채점 Job)
      ├─ preview-hub     hub · bot (+ 환경마다 namespace phub-<이름>)
      └─ thread-example  app · gateway · postgres · traffic · ops   ← 커리큘럼 단계 12 에 맞춰 마지막
```

### 3.1 클러스터

| 항목 | 결정 | 이유 |
| --- | --- | --- |
| 배포판 | k3s (colima `--kubernetes`) | 단일 노드에 가볍다. 지금 쓰는 colima · vz 그대로 |
| 컨테이너 런타임 | docker | `docker build` 한 이미지를 레지스트리 없이 바로 쓴다(`imagePullPolicy: IfNotPresent`) |
| VM 크기 | 12 코어 · 64GB · 디스크 500GB | 아래 쿼터 합 + 여유. 남은 4 코어 · 64GB 는 macOS · earlypay-tests(24GB) · puri |
| 디스크 위치 | `~/.colima/_lima/_disks/colima-k8s` → `/Volumes/TradingData/k8s/colima-disks/` 심볼릭 링크 | judge · puri · thread-example 과 같은 방식. PV 가 모두 SSD 에 생긴다 |
| 스토리지 | k3s 기본 local-path | 단일 노드라 충분. PVC 마다 용량을 적는다 |
| Ingress | Traefik | k3s 기본. colima 가 끄는 경우 0 단계에서 켠다 |

⚠️ 0 단계에서 확인할 것: colima 의 k3s 가 Traefik 을 기본으로 끄는지, docker 런타임에서 k3s 가 로컬 이미지를 그대로 보는지.

### 3.2 외부 노출

```text
*.cafitac.com DNS ─▶ Cloudflare 터널 homelab (1개) ─▶ cloudflared Deployment ─▶ Traefik ─▶ Ingress(호스트명) ─▶ Service
```

- 호스트마다 `cloudflared tunnel route dns --overwrite-dns homelab <host>` 로 **하나씩** 옮긴다. 옛 터널 · 컨테이너는 검증이 끝날 때까지 그대로 둔다 → 롤백 = DNS 를 옛 터널로 되돌리기.
- preview-hub 의 `*.cafitac.com` 와일드카드는 preview-hub 를 옮길 때 같이 옮긴다(그 전까지는 지정 레코드가 와일드카드보다 우선).
- 접근 제한(preview-hub 의 Cloudflare Access)은 Cloudflare 쪽 설정이라 그대로 유지된다.

### 3.3 namespace · 쿼터 (초안)

현재 실측(2026-10-05 `docker stats`)과 컨테이너 상한을 근거로 잡았다. 단위는 limits 합계.

| namespace | CPU | 메모리 | 스토리지 | 근거 |
| --- | --- | --- | --- | --- |
| platform | 1 | 1Gi | – | cloudflared 30MiB 내외 · Traefik |
| monitoring | 2 | 4Gi | 30Gi | Prometheus 보존 15 일 기준 |
| portfolio | 0.5 | 256Mi | – | nginx 6MiB |
| interview-coach | 4 | 12Gi | 20Gi | embedder 상한 7GiB(실사용 4.9GiB) · server 1GiB · knowledge 768MiB · db 1GiB · web 512MiB / pgdata 72MB · 모델 캐시 1.1GB · 백업 |
| judge-board | 상시 2 + 채점 11 | 상시 3Gi + 채점 별도 | 10Gi | board 384MiB · frontend 202MiB · db 62MiB. ⚠️ 공식 채점 한 판 5.5 코어 × 2 줄(README) — 아래 미결 1 |
| preview-hub | 1 + 환경당 2 | 1Gi + 환경당 4Gi | 환경당 10Gi | hub · bot 각 50~70MiB. 환경 수 상한을 둔다 |
| thread-example | 7 | 16Gi | 300Gi | Terraform 예산: app 2코어/4GiB · db 1/2GiB · traffic 2/6GiB · gateway 1/128MiB · ops 2.5GiB / DB 1 억 행 창(ADR 0034) 약 90GB + 여유 |

- 모든 namespace 에 LimitRange(컨테이너 기본 requests · limits) — 상한 없는 컨테이너가 노드를 다 먹지 못하게.
- NetworkPolicy: 기본 거부 + 같은 namespace · Traefik · monitoring 만 허용. 프로젝트끼리는 서로 못 부른다.
- ⚠️ thread-example 의 부하 실험은 「앱 2 코어 · DB 1 코어」 같은 **고정 예산**이 실험 조건이다. k8s 에서는 requests = limits(Guaranteed QoS)로 같은 조건을 만든다.

### 3.4 저장소 · 배포 방식

- 새 저장소 `cafitac/homelab` — 클러스터 설정 · 프로젝트별 매니페스트(kustomize) · 이전 기록.

```text
homelab/
  cluster/        colima 프로필 · k3s 설정 · 0 단계 스크립트
  platform/       cloudflared · Traefik · local-path · namespace 템플릿(쿼터 · LimitRange · NetworkPolicy)
  monitoring/     kube-prometheus-stack values · 경보 규칙
  apps/<프로젝트>/  base + overlays/prod (kustomize)
  docs/           결정 기록 · 이전 기록 · 장애 기록
```

- 이미지: 각 프로젝트 저장소에서 `docker build` (Mac Studio, 클러스터와 같은 docker 런타임) → 태그 = git 커밋. 레지스트리는 두지 않는다(필요해지면 클러스터 안 registry).
- 비밀값: git 에 넣지 않는다. Mac Studio 의 파일에서 `kubectl create secret` 으로 만든다(SOPS · sealed-secrets 는 다른 사람이 쓰게 되면).
- 적용: ⭐ **최종은 Argo CD GitOps** (사용자 결정 2026-10-05). 0 ~ 1 단계는 `kubectl apply -k` 로 시작하고, 플랫폼이 안정되면 Argo CD 를 클러스터에 올려 `homelab` 저장소를 원본으로 삼는다(App of Apps). 이후 프로젝트는 처음부터 Argo CD Application 으로 옮긴다.
- 백업: DB 마다 `pg_dump` CronJob → 백업 PVC(SSD). 지금 compose 의 backup 컨테이너(매일 04:00 · 7 일 보존)를 그대로 옮긴다.

## 4. 단계

각 단계는 **옛 환경을 끄지 않은 채** 새 환경을 띄우고, DNS 를 옮기고, 검증한 뒤에 옛 환경을 정리한다.

### 0 단계 — 클러스터 기반

진행 (2026-10-05):

- [x] colima `k8s` (12 코어 · 64GB · 500GB) + k3s v1.35, 데이터 디스크 SSD — lima 는 디스크 폴더를 미리 링크해 두면 거부한다. 만든 뒤 멈추고 옮긴다(`cluster/up.sh`)
- [x] Traefik 켬 · local-path PV 경로 `/var/lib/rancher/k3s/storage` = SSD 데이터 디스크
- [x] 터널 `homelab` + cloudflared 2 개 — 터널 ID · 자격 증명은 저장소 밖(Secret)
- [x] `hello.cafitac.com` 외부 200 · VM 재시작 뒤 손대지 않고 200 · PV 내용 그대로 · 전역 docker 컨텍스트 그대로
- [x] 격리 컴포넌트(`platform/components/isolation`): 다른 namespace → 차단, 같은 namespace · Traefik → 허용, 쿼터 초과 파드 거부
- [x] monitoring — kube-prometheus-stack 91.9.0 · blackbox(공개 주소 6 개, colima 쪽 서비스 포함) · 규칙 4 개(노드 디스크 80/90% · 사이트 다운 · Prometheus 크기). Prometheus 보존 15 일 · 25GB(PVC 30Gi). Alertmanager 수신처 없음
- [x] 모니터링 도메인 — `grafana` · `prometheus` · `alertmanager.cafitac.com`. Cloudflare Access 앱 `homelab-monitoring`(정책 `owner`, preview-hub 와 같음)을 **먼저** 만들고 DNS 를 붙였다. 인증 없이 열면 Access 로그인(302)
- [x] Grafana 는 Access JWT(`Cf-Access-Jwt-Assertion`, 발급자 = 우리 팀)로 바로 로그인 — 따로 계정 없음. admin 폼은 비상용
- [x] ⚠️ **우회 경로 발견 · 차단**: k3s 기본 Traefik(LoadBalancer + servicelb)이 VM 80/443 에 붙고 lima 가 이를 Mac 의 모든 인터페이스로 넘겨, LAN · tailnet 에서 `Host:` 헤더만으로 Access 없이 Prometheus · Grafana 에 닿았다. Traefik 을 ClusterIP 로 바꿔(`platform/traefik`) Mac:80 리스너가 사라진 것을 확인
- [x] k8s API 는 도메인으로 열지 않는다 — 원격 `kubectl` 은 `cluster/remote-kubeconfig.sh`(ssh 포트 포워딩)
- [x] hello 앱 · `hello.cafitac.com` 레코드 삭제


- colima 프로필 `k8s` (12 코어 · 64GB · 500GB, 디스크 SSD) + k3s
- platform: 터널 `homelab` · cloudflared Deployment · Traefik · local-path 경로 확인
- namespace 템플릿(쿼터 · LimitRange · NetworkPolicy), monitoring(경보 규칙만)
- **통과 기준**: 테스트용 nginx 를 `hello.cafitac.com` 으로 띄워 외부 200 · PVC 가 SSD 에 생김 확인 · VM 재시작 뒤 사람 손 없이 다시 200 · 기본 도커 컨텍스트 그대로

### 1 단계 — portfolio-hub (상태 없음)

- nginx Deployment + ConfigMap(사이트 파일) 또는 이미지
- DNS `portfolio.cafitac.com` → `homelab`
- **통과 기준**: 외부 200 · 이미지 · 캐시 헤더 동일 / **롤백**: DNS 를 `portfolio-hub` 터널로

### 2 단계 — interview-coach (DB · 모델 캐시)

- postgres(pgvector 17) StatefulSet + PVC, 옛 DB 를 `pg_dump` → 복원
- embedder(PVC: Hugging Face 캐시) · knowledge · server · web, 비밀값 Secret
- 백업 CronJob
- **통과 기준**: 로그인 · 지원 목록 · 모의 면접 시작 · 자료 검색이 옛 환경과 같은 데이터로 동작 · 백업 파일 생성 / **롤백**: DNS 를 `interview-coach` 터널로, 이전 중 쓰기는 하지 않는다(쓰기 정지 창을 둔다)
- ⚠️ Claude CLI 구독 토큰을 컨테이너에 넣는 방식은 그대로 옮긴다. API 키 전환(PLAN.md 필수 항목)은 별도 작업

### 3 단계 — preview-hub (재설계)

- 지금: 환경 = docker compose 프로젝트, Traefik(도커 라벨)이 `phub-*.cafitac.com` 분기
- 목표: **환경 = namespace `phub-<이름>`**, 서비스마다 Deployment · Service · Ingress, 환경 namespace 마다 쿼터
- hub 의 runner 를 compose runner 에서 k8s runner(`runner.py` 의 C4 프로토콜 구현 추가)로 — 기존 fake · compose runner 는 유지
- `*.cafitac.com` 와일드카드를 `homelab` 터널로
- ai-qa 는 환경 주소만 맞으면 그대로 동작해야 한다 — 통과 기준에 포함
- **통과 기준**: `phub up` 으로 예시 3 서비스 환경 생성 · 고유 주소 접속 · `phub down` · `gc` 후 namespace 와 PVC 가 남지 않음 · ai-qa 시나리오 통과

### 4 단계 — judge-board (실행기 재설계)

- board · frontend · postgres 는 그대로 옮긴다
- runner: 지금은 도커 소켓으로 샌드박스를 띄운다 → **채점 한 판 = k8s Job**(자원 고정 · 판마다 빈 포트 대신 Pod 네트워크) 으로 바꾸거나, 당분간 DinD 사이드카
- **통과 기준**: 공식 채점 2 줄을 동시에 돌려도 결과가 옛 환경과 같다(같은 제출 · 같은 점수)

### 5 단계 — thread-example (커리큘럼 단계 12 와 함께)

- 이 저장소의 학습 순서에서 Kubernetes 는 단계 12 다. 그 전에 옮기면 학습 단위를 건너뛰게 되므로 **시점은 커리큘럼에 맞춘다.**
- 그때 할 일: Terraform(docker provider) → k8s 매니페스트, 고정 예산 = Guaranteed QoS, DB PVC 300Gi, 트래픽 봇 Deployment, ops(Prometheus · Grafana · console)는 monitoring 과 합칠지 결정
- 그때까지는 colima `thread-example` 프로필을 유지한다(이미 SSD · 300GB). 최종 목표가 「전부 k8s」이므로 단계 12 를 미루지 않고 이 이전에 맞춰 당긴다.

### 6 단계 — 정리

- 옮긴 프로젝트의 colima 프로필 · 터널 삭제, 본체 디스크의 `default`(31GB) · preview-hub 디스크 정리(삭제는 사용자 확인 후)
- 경보 수신처 연결(사용자가 창구를 정하면)

## 5. 위험

| 위험 | 대응 |
| --- | --- |
| 단일 노드 — VM 이 죽으면 전부 죽는다 | 지금도 같다(VM 여러 개지만 호스트 하나). VM 재시작 뒤 자동 복구를 0 단계 통과 기준으로 |
| local-path PV 는 노드에 묶인다 | 단일 노드라 문제없다. 노드를 늘리게 되면 그때 다시 결정 |
| 디스크 500GB 도 언젠가 찬다 | PVC 마다 상한 + 사용률 경보 규칙(수신처는 나중) |
| 이전 중 데이터 유실 | DB 는 쓰기 정지 창 → dump → 복원 → 검증 → DNS. 옛 데이터는 6 단계까지 지우지 않는다 |
| judge-board 공식 채점이 다른 서비스와 CPU 를 다툼 | 채점 Job 에 Guaranteed QoS · 채점 시간대에는 preview 환경 수를 줄인다(미결 1) |

## 5.1 알려진 노출 (남은 것)

- lima 는 VM 에서 모든 주소로 열린 포트를 Mac 의 모든 인터페이스로 넘긴다. k8s VM 에서는 API(55902) · kubelet(10250) 이 그렇게 열려 있다 — 둘 다 인증서 없이는 401 이라 당장 위험은 낮지만 LAN · tailnet 에 보인다. macOS 방화벽(pf) 규칙으로 막는 것을 검토한다(sudo 필요)
- ⭐ 새 서비스에 `hostPort` · `LoadBalancer` · `NodePort` 를 쓰지 않는다 — 같은 경로로 Mac 밖에 열린다. 외부 노출은 오직 cloudflared → Traefik(ClusterIP) → Ingress
- colima 쪽 기존 VM(thread-example 등)도 0.0.0.0 으로 여는 포트가 있다(3000 · 8080 · 9090 등). 그 프로젝트를 옮길 때 함께 정리한다

## 6. 미결 (정해야 할 것)

1. **judge-board 채점 자원** — 공식 채점 2 줄이 11 코어다. 클러스터 12 코어에 넣으면 그 시간엔 다른 것이 거의 못 돈다. ① VM 을 14 코어로 ② 채점 줄을 1 줄로 ③ 채점만 지금처럼 별도 colima 프로필에 남기기
2. **thread-example ops** — 프로젝트 전용 Prometheus · Grafana 를 유지할지, 공용 monitoring 에 합칠지(학습상 분리가 나을 수 있다)
3. ~~Argo CD 도입 시점~~ → 최종 Argo CD 로 결정. 1 단계(portfolio-hub) 뒤에 올리고, 2 단계부터 Argo CD 로 배포한다
4. **puri** — 소유자와 상의해 옮길지, colima 에 남길지
5. ~~`homelab` 저장소 공개 여부~~ → **공개** (사용자 결정 2026-10-05). 비밀값은 계속 git 밖에 두고, 올리기 전에 비밀값 · 내부 주소 검사를 한다. Argo CD 는 공개 저장소를 자격 증명 없이 읽는다

## 7. 포트폴리오로서

- 「여러 사이드 프로젝트를 단일 노드 k8s 에서 namespace · 쿼터로 격리해 운영」 — 실제 트래픽(thread-example 봇 1 만 동시 접속)과 실제 장애(10/3 디스크)를 근거로 옮겼다는 이야기가 된다.
- preview-hub 의 「환경 = namespace」 재설계와 judge-board 의 「채점 = Job」 재설계가 각각 독립된 설계 사례가 된다.
