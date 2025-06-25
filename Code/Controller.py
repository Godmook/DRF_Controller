import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from enum import Enum
import numpy as np
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from pydantic import BaseModel
import os

# Kueue 통합 모듈 import
try:
    from .kueue_integration import KueueJobManager
except ImportError:
    # 로컬 테스트 시 상대 import 실패 방지
    KueueJobManager = None

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class JobPriority(Enum):
    """작업 우선순위 열거형"""
    NORMAL = "Normal"
    URGENT = "Urgent"

class ResourceType(Enum):
    """자원 타입 열거형"""
    CPU = "cpu"
    GPU = "gpu"
    MEMORY = "memory"

class JobInfo(BaseModel):
    """작업 정보 모델"""
    name: str
    namespace: str
    priority: JobPriority
    creation_timestamp: datetime
    resources: Dict[ResourceType, float]
    gang_scheduling: bool = False
    gang_id: Optional[str] = None

class ClusterInfo(BaseModel):
    """클러스터 정보 모델"""
    total_resources: Dict[ResourceType, float]
    allocated_resources: Dict[ResourceType, float]

class DRFScheduler:
    """DRF(Dominant Resource Fairness) 기반 스케줄러"""
    
    def __init__(self, aging_alpha: float = 0.1):
        """
        DRF 스케줄러 초기화
        
        Args:
            aging_alpha: Aging 상수 (기본값: 0.1)
        """
        self.aging_alpha = aging_alpha
        self.cluster_info = None
        
    def calculate_dominant_share(self, job: JobInfo, cluster_info: ClusterInfo) -> float:
        """
        작업의 Dominant Share 계산
        
        Args:
            job: 작업 정보
            cluster_info: 클러스터 정보
            
        Returns:
            Dominant Share 값
        """
        dominant_shares = []
        
        for resource_type, requested_amount in job.resources.items():
            if resource_type in cluster_info.total_resources:
                total_resource = cluster_info.total_resources[resource_type]
                if total_resource > 0:
                    share = requested_amount / total_resource
                    dominant_shares.append(share)
                    logger.info(
                        f"[DEBUG] Job: {job.name}, Resource: {resource_type.name}, "
                        f"Requested: {requested_amount}, Total: {total_resource}, Share: {share:.4f}"
                    )
                else:
                    logger.info(
                        f"[DEBUG] Job: {job.name}, Resource: {resource_type.name}, Total resource is 0."
                    )
            else:
                logger.info(
                    f"[DEBUG] Job: {job.name}, Resource: {resource_type.name} not found in cluster total_resources."
                )
        
        if dominant_shares:
            max_share = max(dominant_shares)
            logger.info(f"[DEBUG] Job: {job.name}, Dominant Shares: {dominant_shares}, Max: {max_share:.4f}")
            return max_share
        else:
            logger.info(f"[DEBUG] Job: {job.name}, No valid dominant shares found. Returning 0.0.")
            return 0.0
    
    def apply_aging(self, dominant_share: float, job: JobInfo) -> float:
        """
        Aging 기법 적용
        
        Args:
            dominant_share: 원래 Dominant Share 값
            job: 작업 정보
            
        Returns:
            Aging이 적용된 Dominant Share 값
        """
        current_time = datetime.now()
        time_diff = (current_time - job.creation_timestamp).total_seconds() / 3600  # 시간 단위
        
        # Aging 적용: 시간이 지날수록 dominant share가 감소하여 우선순위가 높아짐
        aging_factor = self.aging_alpha * time_diff
        adjusted_share = max(0.0, dominant_share - aging_factor)
        
        return adjusted_share
    
    def calculate_priority_score(self, job: JobInfo, cluster_info: ClusterInfo) -> float:
        """
        작업의 우선순위 점수 계산
        
        Args:
            job: 작업 정보
            cluster_info: 클러스터 정보
            
        Returns:
            우선순위 점수 (낮을수록 높은 우선순위)
        """
        # 기본 DRF 계산
        dominant_share = self.calculate_dominant_share(job, cluster_info)
        
        # Aging Factor 계산 (초 단위로 정확하게 계산)
        current_time = datetime.now()
        time_diff = (current_time - job.creation_timestamp).total_seconds()  # 초 단위
        aging_factor = self.aging_alpha * time_diff
        
        # 우선순위 가중치 적용 (Urgent: 0, Normal: 1000)
        priority_weight = 0 if job.priority == JobPriority.URGENT else 1000
        
        # 최종 점수: Priority Weight + Dominant Share - Aging Factor
        final_score = priority_weight + dominant_share - aging_factor
        
        logger.info(
            f"[DEBUG] Job: {job.name}, Priority: {job.priority}, "
            f"Dominant Share: {dominant_share:.4f}, Aging Factor: {aging_factor:.6f}, "
            f"Priority Weight: {priority_weight}, Final Score: {final_score:.4f} "
            f"(Weight + DOM - Aging = {priority_weight} + {dominant_share:.4f} - {aging_factor:.6f})"
        )
        
        return final_score

class K8SController:
    """Kubernetes 컨트롤러"""
    
    def __init__(self, kueue_enabled: bool = True, scheduling_interval: int = 30):
        """
        K8S 컨트롤러 초기화
        
        Args:
            kueue_enabled: Kueue 통합 활성화 여부
            scheduling_interval: 스케줄링 간격 (초)
        """
        try:
            config.load_incluster_config()  # 클러스터 내부에서 실행될 때
        except config.ConfigException:
            config.load_kube_config()  # 로컬 개발 환경
        
        _safe_load_kube_config()
        self.core_v1 = client.CoreV1Api()
        self.batch_v1 = client.BatchV1Api()
        self.custom_objects_api = client.CustomObjectsApi()
        self.drf_scheduler = DRFScheduler()
        
        # Kueue 통합 설정
        self.kueue_enabled = kueue_enabled
        if self.kueue_enabled:
            try:
                self.kueue_manager = KueueJobManager()
                logger.info("Kueue integration enabled")
            except Exception as e:
                logger.warning(f"Failed to initialize Kueue integration: {e}")
                self.kueue_manager = None
        else:
            self.kueue_manager = None
            logger.info("Kueue integration disabled")
        
        self.scheduling_interval = scheduling_interval
        
    async def get_pending_jobs(self) -> List[JobInfo]:
        """
        Pending 상태의 작업들을 가져옴
        
        Returns:
            Pending 작업 목록
        """
        pending_jobs = []
        
        try:
            # 모든 네임스페이스에서 Job 조회
            jobs = self.batch_v1.list_job_for_all_namespaces()
            
            for job in jobs.items:
                if job.status.phase == "Pending" or not job.status.conditions:
                    # 작업 정보 추출
                    job_info = await self._extract_job_info(job)
                    if job_info:
                        pending_jobs.append(job_info)
                        
        except ApiException as e:
            logger.error(f"Failed to get pending jobs: {e}")
            
        return pending_jobs
    
    async def _extract_job_info(self, job) -> Optional[JobInfo]:
        """
        K8S Job에서 JobInfo 추출
        
        Args:
            job: K8S Job 객체
            
        Returns:
            JobInfo 객체 또는 None
        """
        try:
            # 기본 정보 추출
            name = job.metadata.name
            namespace = job.metadata.namespace
            creation_timestamp = job.metadata.creation_timestamp
            
            # 우선순위 결정
            priority = JobPriority.NORMAL
            if job.metadata.annotations and job.metadata.annotations.get("priority") == "approved":
                priority = JobPriority.URGENT
            
            # 자원 요구사항 추출
            resources = {}
            if job.spec.template.spec.containers:
                container = job.spec.template.spec.containers[0]
                if container.resources and container.resources.requests:
                    requests = container.resources.requests
                    
                    if hasattr(requests, 'cpu'):
                        resources[ResourceType.CPU] = self._parse_cpu_request(requests.cpu)
                    if hasattr(requests, 'memory'):
                        resources[ResourceType.MEMORY] = self._parse_memory_request(requests.memory)
                    if hasattr(requests, 'nvidia.com/gpu'):
                        resources[ResourceType.GPU] = float(requests['nvidia.com/gpu'])
            
            # Gang Scheduling 확인
            gang_scheduling = False
            gang_id = None
            if job.metadata.annotations:
                gang_scheduling = job.metadata.annotations.get("gang-scheduling", "false").lower() == "true"
                gang_id = job.metadata.annotations.get("gang-id")
            
            return JobInfo(
                name=name,
                namespace=namespace,
                priority=priority,
                creation_timestamp=creation_timestamp,
                resources=resources,
                gang_scheduling=gang_scheduling,
                gang_id=gang_id
            )
            
        except Exception as e:
            logger.error(f"Failed to extract job info: {e}")
            return None
    
    def _parse_cpu_request(self, cpu_str: str) -> float:
        """CPU 요청량 파싱"""
        if cpu_str.endswith('m'):
            return float(cpu_str[:-1]) / 1000
        return float(cpu_str)
    
    def _parse_memory_request(self, memory_str: str) -> float:
        """메모리 요청량 파싱 (MB 단위)"""
        if memory_str.endswith('Ki'):
            return float(memory_str[:-2]) / 1024
        elif memory_str.endswith('Mi'):
            return float(memory_str[:-2])
        elif memory_str.endswith('Gi'):
            return float(memory_str[:-2]) * 1024
        elif memory_str.endswith('Ti'):
            return float(memory_str[:-2]) * 1024 * 1024
        else:
            return float(memory_str) / (1024 * 1024)  # bytes to MB
    
    async def get_cluster_info(self) -> ClusterInfo:
        """
        클러스터 자원 정보 조회
        
        Returns:
            클러스터 정보
        """
        try:
            # 노드 정보 조회
            nodes = self.core_v1.list_node()
            
            total_resources = {
                ResourceType.CPU.value: 0.0,
                ResourceType.MEMORY.value: 0.0,
                ResourceType.GPU.value: 0.0
            }
            
            allocated_resources = {
                ResourceType.CPU.value: 0.0,
                ResourceType.MEMORY.value: 0.0,
                ResourceType.GPU.value: 0.0
            }
            
            for node in nodes.items:
                # 총 자원량
                if node.status.capacity:
                    capacity = node.status.capacity
                    if hasattr(capacity, 'cpu'):
                        total_resources[ResourceType.CPU.value] += self._parse_cpu_request(capacity.cpu)
                    if hasattr(capacity, 'memory'):
                        total_resources[ResourceType.MEMORY.value] += self._parse_memory_request(capacity.memory)
                    if hasattr(capacity, 'nvidia.com/gpu'):
                        total_resources[ResourceType.GPU.value] += float(capacity['nvidia.com/gpu'])
                
                # 할당된 자원량
                if node.status.allocatable:
                    allocatable = node.status.allocatable
                    if hasattr(allocatable, 'cpu'):
                        allocated_resources[ResourceType.CPU.value] += self._parse_cpu_request(allocatable.cpu)
                    if hasattr(allocatable, 'memory'):
                        allocated_resources[ResourceType.MEMORY.value] += self._parse_memory_request(allocatable.memory)
                    if hasattr(allocatable, 'nvidia.com/gpu'):
                        allocated_resources[ResourceType.GPU.value] += float(allocatable['nvidia.com/gpu'])
            
            return ClusterInfo(
                total_resources=total_resources,
                allocated_resources=allocated_resources
            )
            
        except ApiException as e:
            logger.error(f"Failed to get cluster info: {e}")
            return None
    
    def handle_gang_scheduling(self, jobs: List[JobInfo]) -> List[JobInfo]:
        """
        Gang Scheduling 처리
        
        Args:
            jobs: 작업 목록
            
        Returns:
            Gang Scheduling이 적용된 작업 목록
        """
        gang_groups = {}
        
        # Gang ID별로 작업 그룹화
        for job in jobs:
            if job.gang_scheduling and job.gang_id:
                if job.gang_id not in gang_groups:
                    gang_groups[job.gang_id] = []
                gang_groups[job.gang_id].append(job)
        
        # Gang 그룹의 모든 작업이 준비되었는지 확인
        ready_gangs = []
        for gang_id, gang_jobs in gang_groups.items():
            # 모든 Gang 작업이 Pending 상태인지 확인
            if all(job for job in gang_jobs):
                ready_gangs.extend(gang_jobs)
        
        # Gang Scheduling이 아닌 작업들 추가
        non_gang_jobs = [job for job in jobs if not job.gang_scheduling]
        
        return ready_gangs + non_gang_jobs
    
    async def calculate_job_priorities(self) -> List[Tuple[JobInfo, float]]:
        """
        작업들의 우선순위 계산
        
        Returns:
            (작업, 우선순위 점수) 튜플 리스트
        """
        # Pending 작업들 가져오기
        pending_jobs = await self.get_pending_jobs()
        
        if not pending_jobs:
            return []
        
        # 클러스터 정보 가져오기
        cluster_info = await self.get_cluster_info()
        if not cluster_info:
            logger.error("Failed to get cluster info")
            return []
        
        # Gang Scheduling 처리
        schedulable_jobs = self.handle_gang_scheduling(pending_jobs)
        
        # 우선순위 계산
        job_priorities = []
        for job in schedulable_jobs:
            priority_score = self.drf_scheduler.calculate_priority_score(job, cluster_info)
            job_priorities.append((job, priority_score))
        
        # 우선순위 점수로 정렬 (낮은 점수가 높은 우선순위)
        job_priorities.sort(key=lambda x: x[1])
        
        return job_priorities
    
    async def update_kueue_priorities(self, job_priorities: List[Tuple[JobInfo, float]]):
        """
        Kueue 우선순위 업데이트
        
        Args:
            job_priorities: (작업, 우선순위 점수) 튜플 리스트
        """
        if not self.kueue_manager:
            logger.warning("Kueue job manager not initialized")
            return
        
        await self.kueue_manager.update_all_job_priorities(job_priorities)

class DRFController:
    """DRF 기반 스케줄링 컨트롤러 메인 클래스"""
    
    def __init__(self, kueue_enabled: bool = True, scheduling_interval: int = 30):
        """
        DRF 컨트롤러 초기화
        
        Args:
            kueue_enabled: Kueue 통합 활성화 여부
            scheduling_interval: 스케줄링 간격 (초)
        """
        self.k8s_controller = K8SController(kueue_enabled, scheduling_interval)
        self.scheduling_interval = scheduling_interval
        self.running = False
    
    async def start(self):
        """컨트롤러 시작"""
        self.running = True
        logger.info("DRF Controller started")
        
        while self.running:
            try:
                # 작업 우선순위 계산
                job_priorities = await self.k8s_controller.calculate_job_priorities()
                
                if job_priorities:
                    logger.info(f"Calculated priorities for {len(job_priorities)} jobs")
                    
                    # Kueue 우선순위 업데이트
                    await self.k8s_controller.update_kueue_priorities(job_priorities)
                else:
                    logger.info("No pending jobs found")
                
                # 대기
                await asyncio.sleep(self.scheduling_interval)
                
            except Exception as e:
                logger.error(f"Error in controller loop: {e}")
                await asyncio.sleep(self.scheduling_interval)
    
    def stop(self):
        """컨트롤러 중지"""
        self.running = False
        logger.info("DRF Controller stopped")

# 메인 실행 함수
async def main():
    """메인 함수"""
    # 환경 변수에서 설정 읽기
    kueue_enabled = os.getenv('KUEUE_ENABLED', 'true').lower() == 'true'
    scheduling_interval = int(os.getenv('SCHEDULING_INTERVAL', '30'))
    
    logger.info(f"Starting DRF Controller - Kueue enabled: {kueue_enabled}, Interval: {scheduling_interval}s")
    
    controller = DRFController(
        kueue_enabled=kueue_enabled,
        scheduling_interval=scheduling_interval
    )
    
    try:
        await controller.start()
    except KeyboardInterrupt:
        logger.info("Shutting down DRF Controller...")
        controller.stop()
    except Exception as e:
        logger.error(f"Fatal error in DRF Controller: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())
