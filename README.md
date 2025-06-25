# DRF Controller for Kubernetes with Kueue Integration

Dominant Resource Fairness (DRF) 기반의 Kubernetes 스케줄러 컨트롤러로, Kueue와 통합하여 고급 작업 스케줄링을 제공합니다.

## 🚀 주요 기능

- **DRF (Dominant Resource Fairness) 스케줄링**: 공정한 리소스 분배
- **Aging 메커니즘**: Starvation 방지를 위한 노화 기반 우선순위 조정
- **Kueue 통합**: Kueue Workload API를 통한 우선순위 관리
- **Gang Scheduling**: 연관 작업들의 동시 실행 보장
- **실시간 모니터링**: 작업 상태 및 리소스 사용량 추적

## 🏗️ 아키텍처

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   DRF Controller│    │   Kueue API     │    │   Kubernetes    │
│                 │    │                 │    │   Cluster       │
│ • Job Monitoring│◄──►│ • Workload Mgmt │◄──►│ • Pod Scheduling│
│ • Priority Calc │    │ • Queue Status  │    │ • Resource Mgmt │
│ • Aging Logic   │    │ • Priority Sync │    │ • Node Status   │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

## 📋 요구사항

- Kubernetes 1.20+
- Kueue 0.4.0+
- Python 3.8+
- Docker

## 🛠️ 설치 및 배포

### 1. 소스 코드 빌드

```bash
# 프로젝트 클론
git clone <repository-url>
cd DRF_Controller

# Docker 이미지 빌드
docker build -t drf-controller:latest .
```

### 2. Kubernetes 배포

```bash
# 네임스페이스 생성
kubectl apply -f k8s/namespace.yaml

# ConfigMap 적용
kubectl apply -f k8s/configmap.yaml

# Deployment 배포
kubectl apply -f k8s/deployment.yaml

# 상태 확인
kubectl get pods -n drf-system
kubectl logs -f deployment/drf-controller -n drf-system
```

### 3. 환경 변수 설정

| 변수명 | 기본값 | 설명 |
|--------|--------|------|
| `KUEUE_ENABLED` | `true` | Kueue 통합 활성화 |
| `SCHEDULING_INTERVAL` | `30` | 스케줄링 간격 (초) |
| `LOG_LEVEL` | `INFO` | 로그 레벨 |

## 🔧 설정

### ConfigMap 설정

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: drf-controller-config
  namespace: drf-system
data:
  aging_alpha: "0.1"
  max_aging_hours: "336"  # 14일
  priority_weights:
    urgent: "0"
    normal: "1000"
```

### Kueue Queue 설정

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: Queue
metadata:
  name: default
spec:
  clusterQueue: default-cluster-queue
```

## 📊 우선순위 계산 공식

```
Priority Score = Priority Weight + Dominant Share - Aging Factor

Where:
- Priority Weight: Urgent=0, Normal=1000
- Dominant Share: DRF 기반 리소스 점유율
- Aging Factor: (현재시간 - 생성시간) × aging_alpha
```

## 🔍 모니터링

### 로그 확인

```bash
# 실시간 로그
kubectl logs -f deployment/drf-controller -n drf-system

# 특정 시간대 로그
kubectl logs deployment/drf-controller -n drf-system --since=1h
```

### 메트릭 확인

```bash
# Pod 상태
kubectl get pods -n drf-system

# Kueue Workload 상태
kubectl get workloads -A

# Queue 상태
kubectl get queues -A
```

## 🧪 테스트

### 로컬 테스트

```bash
# 테스트 실행
python -m pytest Code/test_controller.py -v

# 개별 테스트
python Code/test_controller.py
```

### 통합 테스트

```bash
# Kueue 통합 테스트
kubectl exec -it deployment/drf-controller -n drf-system -- python -c "
from Code.test_controller import test_kueue_integration
import asyncio
asyncio.run(test_kueue_integration())
"
```

## 🔧 문제 해결

### 일반적인 문제들

1. **Kueue API 연결 실패**
   ```bash
   # RBAC 권한 확인
   kubectl auth can-i get workloads --all-namespaces
   
   # ServiceAccount 확인
   kubectl get serviceaccount drf-controller -n drf-system
   ```

2. **우선순위 업데이트 실패**
   ```bash
   # Workload 상태 확인
   kubectl get workloads -A -o wide
   
   # 컨트롤러 로그 확인
   kubectl logs deployment/drf-controller -n drf-system | grep "priority"
   ```

3. **리소스 부족**
   ```bash
   # 노드 리소스 확인
   kubectl top nodes
   
   # Pod 리소스 확인
   kubectl top pods -n drf-system
   ```

## 📈 성능 최적화

### 권장 설정

- **스케줄링 간격**: 30초 (대부분의 환경에 적합)
- **Aging Alpha**: 0.1 (적당한 노화 속도)
- **메모리 제한**: 256Mi (기본값)
- **CPU 제한**: 200m (기본값)

### 대용량 클러스터 설정

```yaml
resources:
  requests:
    memory: "512Mi"
    cpu: "500m"
  limits:
    memory: "1Gi"
    cpu: "1000m"
```

## 🤝 기여하기

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## 📄 라이선스

MIT License

## 📞 지원

문제가 발생하거나 질문이 있으시면 이슈를 생성해 주세요. 