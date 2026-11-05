# skills 러너 토큰 폐기 절차 (S10)

러너가 쓰는 Claude 구독 토큰이 새거나 새었다고 의심될 때의 순서다. 이 문서에는 값이 없다. 모든 명령은 owner 가 Mac Studio 에서 실행한다 — 에이전트는 실행하지 않는다.

## 왜 이 순서인가

- 구독 토큰 값 하나를 네 서비스가 같이 쓴다(interview-coach · trading · ai-daily · skills 러너). 토큰을 폐기하면 네 곳이 한꺼번에 멈추므로, 새 값을 만든 뒤 사용자에게 보이는 서비스부터 되살린다.
- 토큰 단위 폐기 방법이 있는지는 게이트 증명 3(U19s)이 확인한다. 증명이 끝나기 전에는 이 절차를 실제 폐기에 쓰지 않고, 아래 7 번의 모의 훈련으로만 돌린다.
- 값은 어느 단계에서도 화면 · 저장소 · 명령줄에 남기지 않는다. 새 값은 SSD 의 서비스별 env 파일에만 둔다(0600).

## 절차

1. 멈춘다.
   - 스튜디오 설정 화면에서 `studio.runner.accept` 를 끈다.
   - Argo 가 replicas 를 git 값으로 되돌리므로(selfHeal) 자동 동기화를 먼저 멈춘다. root 가 skills-runner Application 을 git 에서 되살리므로 root 부터 멈춘다.
     - `kubectl -n argocd patch app root --type merge -p '{"spec":{"syncPolicy":null}}'`
     - `kubectl -n argocd patch app skills-runner --type merge -p '{"spec":{"syncPolicy":null}}'`
   - `kubectl -n skills-runner scale deploy/runner --replicas=0`
2. 새 토큰을 만든다 — owner 기기에서 `claude setup-token`. 발급일을 기록한다.
3. 옛 토큰을 증명 3 에서 확인한 방법으로 폐기한다(토큰 단위 방법이 없으면 계정 전체 로그아웃뿐이며, 이는 네 서비스와 owner 의 대화형 로그인을 모두 멈추는 비상 수단이다).
4. 새 값을 서비스별 env 파일에 쓰고, 아래 순서대로 각 `secrets.sh` 를 다시 실행한다. 사용자에게 보이는 서비스를 먼저 되살린다.
   1. interview-coach — `apps/interview-coach/secrets.sh`, 이어서 `kubectl -n interview-coach rollout restart deploy/server`
   2. trading — `apps/trading/secrets.sh`(다음 Job 부터 새 값을 읽는다)
   3. ai-daily — `apps/ai-daily/secrets.sh`(다음 CronJob 부터 새 값을 읽는다)
   4. skills 러너 — `apps/skills-runner/secrets.sh`(env 파일의 `CLAUDE_CODE_OAUTH_TOKEN` 과 `ISSUED` 를 새 값으로 바꾼 뒤)
5. 서비스마다 모델 호출 한 번 또는 생존 게이지로 확인한다. 이 확인을 위해 다른 서비스에 추가 호출을 만들지 않는다.
6. GitHub 세분화된 PAT 도 폐기한다 — GitHub 설정 → Developer settings → Fine-grained tokens 에서 삭제한다. 같은 범위(저장소 두 개, Contents · Pull requests 쓰기, Metadata 읽기, 90 일)로 새로 발급해 env 파일의 `GH_TOKEN` 을 바꾸고 `apps/skills-runner/secrets.sh` 를 다시 실행한다.
7. 날짜 · 방법 · 확인한 서비스를 상위 control record 에 적는다. `studio.revocation.verified_at` 은 throwaway 토큰으로 위 절차를 끝까지 돌린 훈련이 성공했을 때에만 설정한다.

## 다시 켜기

- `studio.runner.accept` 는 `studio.revocation.verified_at` 이 있어야 켜진다(AcceptGate).
- 웹 파드는 Secret 이 바뀌어도 HMAC 키 파일을 다시 읽지 않을 수 있으므로, 키를 바꿨다면 `kubectl -n skills rollout restart deploy/web` 을 한다.
- 자동 동기화를 되살린다(역순): `kubectl apply -f argocd/apps/skills-runner.yaml`, 이어서 `kubectl apply -f argocd/root.yaml`. 러너 replicas 는 git 이 정한다 — 다시 1 로 올리는 것은 `scale` 이 아니라 homelab PR 이다(동기화를 되살리기 전에 git 값이 0 인지 확인한다).
- HMAC 키만 바꾸는 경우: env 파일의 `RUNNER_HMAC_KEY` 를 새로 만들고(`secrets.sh` 머리 주석 참고) `secrets.sh` 를 실행한다. 이 키는 두 namespace 에 같은 값으로 들어간다.

## 점검

- 이 문서와 `secrets.sh` 는 공개 저장소에 있다. 값 · 계정 이름 · 식별자를 적지 않는다.
- 정기 훈련: 증명 3 의 throwaway 토큰으로 위 절차를 한 번 돌리고 7 번을 기록한다.
