"""衡阳新能源联合消纳计划服务。

基于带版本的发电预测、储能可用窗口、线路输变电能力和工业用户保供承诺，
生成可追溯的分时联合消纳方案，并提供资源锁定、预测修订重算、
调度指令事件序号核对和重启恢复能力。
"""

from .service import JointDispatchService

__all__ = ["JointDispatchService"]
