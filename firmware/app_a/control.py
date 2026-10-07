"""温度曲线策略占位：本阶段不假定最终控制算法，也不在模拟采样中调用。"""


def compute_group_targets(temperature_c, curve, previous_targets):
    """未来返回两组目标 PWM 或 RPM；目标单位确定后再固定接口与持久化格式。"""
    # 伪代码：
    # 1. 检查温度读数是否有效、是否过期；异常时进入经验证的故障保护策略。
    # 2. 验证用户曲线点有序且数值合法，再按温度区间插值计算目标。
    # 3. 根据需求限制最低转速（文档当前为 20%），并加入迟滞/变化速率限制。
    # 4. 若最终目标为 RPM，还需以真实测速反馈调节 PWM，而非简单等同两者。
    # 5. 返回两组目标；由硬件接口执行，独立监控每个风扇是否失速。
    raise NotImplementedError("Temperature-to-fan control policy is not implemented")
