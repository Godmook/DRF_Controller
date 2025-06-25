import logging
from typing import List, Tuple, Optional, Dict, Any
from kubernetes import client
from kubernetes.client.rest import ApiException
from .Controller import JobInfo
import concurrent.futures

logger = logging.getLogger(__name__)

class KueueIntegration:
    """Kueue와의 통합을 위한 클래스"""
    
    def __init__(self):
        """Kueue 통합 초기화"""
        self.custom_objects_api = client.CustomObjectsApi()
        
    def update_workload_priority(self, job: JobInfo, priority_score: float, rank: int) -> bool:
        """
        Kueue Workload의 우선순위 업데이트
        
        Args:
            job: 작업 정보
            priority_score: 우선순위 점수
            rank: 순위
            
        Returns:
            업데이트 성공 여부
        """
        try:
            # Kueue Workload 조회
            workload = self._get_workload_for_job(job)
            if not workload:
                logger.warning(f"No Kueue workload found for job {job.name}")
                return False
            
            # Workload 우선순위 업데이트
            self._update_workload_priority(workload, priority_score, rank)
            
            logger.info(f"Updated Kueue workload priority for job {job.name}: {priority_score:.4f}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to update Kueue workload priority for job {job.name}: {e}")
            return False
    
    def _get_workload_for_job(self, job: JobInfo) -> Optional[Dict[str, Any]]:
        """
        Job에 해당하는 Kueue Workload 조회
        
        Args:
            job: 작업 정보
            
        Returns:
            Kueue Workload 객체 또는 None
        """
        try:
            # 모든 네임스페이스에서 Workload 조회
            workloads = self.custom_objects_api.list_cluster_custom_object(
                group="kueue.x-k8s.io",
                version="v1beta1",
                plural="workloads"
            )
            
            # Job 이름과 매칭되는 Workload 찾기
            for workload in workloads.get('items', []):
                metadata = workload.get('metadata', {})
                workload_name = metadata.get('name', '')
                
                # Workload 이름이 Job 이름을 포함하는지 확인
                if job.name in workload_name:
                    return workload
            
            return None
            
        except ApiException as e:
            logger.error(f"Failed to get Kueue workloads: {e}")
            return None
    
    def _update_workload_priority(self, workload: Dict[str, Any], priority_score: float, rank: int):
        """
        Kueue Workload의 우선순위 업데이트
        
        Args:
            workload: Kueue Workload 객체
            priority_score: 우선순위 점수
            rank: 순위
        """
        try:
            workload_name = workload['metadata']['name']
            workload_namespace = workload['metadata']['namespace']
            
            # 우선순위 정보를 Workload의 annotations에 추가
            patch_body = {
                "metadata": {
                    "annotations": {
                        "drf-scheduler/priority-score": str(priority_score),
                        "drf-scheduler/rank": str(rank),
                        "drf-scheduler/updated-by": "drf-controller"
                    }
                },
                "spec": {
                    "priority": int(priority_score * 1000)  # Kueue priority field
                }
            }
            
            # Workload 업데이트
            self.custom_objects_api.patch_namespaced_custom_object(
                group="kueue.x-k8s.io",
                version="v1beta1",
                namespace=workload_namespace,
                plural="workloads",
                name=workload_name,
                body=patch_body
            )
            
            logger.info(f"Updated Kueue workload {workload_name} with priority {priority_score:.4f}")
            
        except Exception as e:
            logger.error(f"Failed to update Kueue workload: {e}")
            raise
    
    def get_queue_status(self, queue_name: str = "default") -> Optional[Dict[str, Any]]:
        """
        Kueue Queue 상태 조회
        
        Args:
            queue_name: Queue 이름
            
        Returns:
            Queue 상태 정보 또는 None
        """
        try:
            queue = self.custom_objects_api.get_cluster_custom_object(
                group="kueue.x-k8s.io",
                version="v1beta1",
                plural="queues",
                name=queue_name
            )
            
            return queue
            
        except ApiException as e:
            logger.error(f"Failed to get Kueue queue {queue_name}: {e}")
            return None
    
    def get_pending_workloads(self) -> List[Dict[str, Any]]:
        """
        Pending 상태의 Kueue Workload 조회
        
        Returns:
            Pending Workload 목록
        """
        try:
            workloads = self.custom_objects_api.list_cluster_custom_object(
                group="kueue.x-k8s.io",
                version="v1beta1",
                plural="workloads"
            )
            
            pending_workloads = []
            for workload in workloads.get('items', []):
                status = workload.get('status', {})
                conditions = status.get('conditions', [])
                
                # Pending 상태 확인
                is_pending = True
                for condition in conditions:
                    if condition.get('type') == 'Admitted' and condition.get('status') == 'True':
                        is_pending = False
                        break
                
                if is_pending:
                    pending_workloads.append(workload)
            
            return pending_workloads
            
        except ApiException as e:
            logger.error(f"Failed to get pending Kueue workloads: {e}")
            return []

class KueueJobManager:
    """Kueue Job 관리 클래스"""
    
    def __init__(self):
        """Kueue Job 관리자 초기화"""
        self.kueue_integration = KueueIntegration()
        
    async def update_all_job_priorities(self, job_priorities: List[Tuple[JobInfo, float]]):
        """
        모든 작업의 우선순위 업데이트 (10개씩 병렬 patch, 우선순위 높은 순서대로)
        
        Args:
            job_priorities: (작업, 우선순위 점수) 튜플 리스트
        """
        # 우선순위가 높은 순서대로 정렬 (낮은 점수가 높은 우선순위)
        job_priorities = sorted(job_priorities, key=lambda x: x[1])
        success_count = 0
        total_count = len(job_priorities)
        batch_size = 10
        
        def patch_job(args):
            rank, (job, priority_score) = args
            return self.kueue_integration.update_workload_priority(job, priority_score, rank)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as executor:
            for i in range(0, total_count, batch_size):
                batch = list(enumerate(job_priorities[i:i+batch_size], start=i+1))
                results = list(executor.map(patch_job, batch))
                success_count += sum(results)
        
        logger.info(f"Updated Kueue priorities for {success_count}/{total_count} jobs (batch size: {batch_size})")
    
    def get_job_queue_status(self, job: JobInfo) -> Optional[dict]:
        """
        작업의 큐 상태 조회
        
        Args:
            job: 작업 정보
            
        Returns:
            큐 상태 정보 또는 None
        """
        try:
            # Job에 해당하는 Workload 조회
            workload = self.kueue_integration._get_workload_for_job(job)
            if workload:
                return {
                    "workload_name": workload.get('metadata', {}).get('name'),
                    "status": workload.get('status', {}),
                    "queue": workload.get('spec', {}).get('queueName')
                }
            return None
            
        except Exception as e:
            logger.error(f"Failed to get queue status for job {job.name}: {e}")
            return None
    
    def get_pending_workloads_info(self) -> List[Dict[str, Any]]:
        """
        Pending Workload 정보 조회
        
        Returns:
            Pending Workload 정보 목록
        """
        try:
            pending_workloads = self.kueue_integration.get_pending_workloads()
            
            workload_info = []
            for workload in pending_workloads:
                metadata = workload.get('metadata', {})
                spec = workload.get('spec', {})
                status = workload.get('status', {})
                
                info = {
                    "name": metadata.get('name'),
                    "namespace": metadata.get('namespace'),
                    "queue": spec.get('queueName'),
                    "priority": spec.get('priority', 0),
                    "status": status.get('conditions', [])
                }
                workload_info.append(info)
            
            return workload_info
            
        except Exception as e:
            logger.error(f"Failed to get pending workloads info: {e}")
            return [] 