# homelab

Mac Studio 의 사이드 프로젝트를 k3s 단일 노드 클러스터 하나로 운영한다. 계획과 단계는 [PLAN.md](PLAN.md).

```
cluster/     k8s 노드 VM(colima 프로필 k8s) 기동
```

Mac Studio 에서:

```bash
bash ~/Project/homelab/cluster/up.sh
export KUBECONFIG=~/.kube/homelab.yaml
kubectl get nodes
```

로컬에서 Mac Studio 로 동기화: `rsync -az --delete --exclude .git ./ trading-macstudio:Project/homelab/`
