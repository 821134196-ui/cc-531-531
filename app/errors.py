"""业务错误类型。"""


class DomainError(Exception):
    """业务规则拒绝（400）。"""


class ConservationError(DomainError):
    """数量对不上。"""


class OverdueError(DomainError):
    """储存超期阻断（400，需先有处置记录）。"""


class ConflictError(DomainError):
    """盘位冲突 / 并发转盘冲突（409）。"""


class PermissionError(DomainError):
    """角色权限不足（403）。"""


class NotFound(DomainError):
    """资源不存在（404）。"""
