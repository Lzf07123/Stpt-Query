"""网络日历订阅：只读 ICS 订阅、加密凭据托管与刷新调度。

模块划分：
- config：节次时间表/学期基准（config/calendar.json，人工维护、启动即校验）
- rules：周次解析、单节识别、教学周→日期展开
- crypto：学号 HMAC 索引、AES-GCM 加解密、订阅令牌与短时验证令牌
- ics：RFC 5545 生成（事件、UID、SEQUENCE、时区）
- store：SQLite 权威存储（订阅、幂等键、审计）
- service：业务闭环（开启/状态识别/刷新/轮换/关闭）
- scheduler：后台刷新（next_refresh_at 原子抢占）
- routes：对外 API、订阅源与后台管理接口
"""

__all__ = ["config", "rules", "crypto", "ics", "store", "service", "scheduler", "routes"]
