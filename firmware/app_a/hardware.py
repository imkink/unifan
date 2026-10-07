"""温湿度与风扇接口：模拟实现可运行，真实实现只有中文伪代码和显式占位。

本模块不导入 machine、不接触 GPIO；模拟 RPM 绝不能解释为物理风扇转速。
真实电路就绪后实现下面两个 Hardware 类，上层继续调用相同的方法。
"""

import app_config


def _number(value, lower, upper, label):
    # 排除布尔值、NaN 和越界值，避免模拟接口输出不合法 JSON 数值。
    if type(value) not in (int, float) or not lower <= value <= upper:
        raise ValueError("Invalid " + label)


class SimulatedSensor:
    simulated = True

    def __init__(self, temperature, humidity):
        self.set_environment(temperature, humidity)

    async def initialize(self):
        # 与未来异步传感器初始化保持相同接口；模拟模式没有总线操作。
        pass

    def set_environment(self, temperature, humidity):
        """测试/联调时显式改变模拟输入；不自动生成随机数据，便于复现。"""
        _number(temperature, -40, 125, "simulated temperature")
        _number(humidity, 0, 100, "simulated humidity")
        self.temperature = temperature
        self.humidity = humidity

    async def read(self):
        return {"temperature_c": self.temperature, "humidity_percent": self.humidity,
                "simulated": True}


class SimulatedFans:
    simulated = True

    def __init__(self, groups, initial_pwm, full_rpm):
        if tuple(groups) != (1, 1, 1, 1, 2, 2) or len(initial_pwm) != 2:
            raise ValueError("Expected six fans in two PWM groups")
        _number(full_rpm, 1, 100000, "simulated full-speed RPM")
        self.groups = tuple(groups)
        self.full_rpm = full_rpm
        self.pwm = [0, 0]
        for group, value in enumerate(initial_pwm, 1):
            self.set_group_pwm(group, value)

    async def initialize(self):
        pass

    def set_group_pwm(self, group, percent):
        """只修改模拟组设定；同组风扇共享 PWM，不能分别指定六路 PWM。"""
        if type(group) is not int or group not in (1, 2):
            raise ValueError("PWM group must be 1 or 2")
        _number(percent, 0, 100, "PWM percent")
        self.pwm[group - 1] = percent

    async def read(self):
        # 结构上保留六路独立转速；目前线性公式只是演示，不是闭环测速。
        return [{"id": index + 1, "group": group,
                 "pwm_percent": self.pwm[group - 1],
                 "rpm": int(self.full_rpm * self.pwm[group - 1] / 100),
                 "simulated": True} for index, group in enumerate(self.groups)]


class HardwareSensor:
    simulated = False

    async def initialize(self):
        # 伪代码：建立 ENV-III 的 I2C -> 核对器件地址/身份 -> 自检 -> 设置转换周期。
        # 就绪后再接入 machine.I2C；地址、校验与超时以实际器件和电路验证为准。
        raise NotImplementedError("ENV-III hardware driver is not implemented")

    async def read(self):
        # 伪代码：触发测量 -> 异步等待转换 -> 读取/校验 -> 返回温湿度和 simulated=False。
        # 失败应报告错误，不得用旧值或模拟值冒充本次真实读数。
        raise NotImplementedError("ENV-III sampling is not implemented")


class HardwareFans:
    simulated = False

    async def initialize(self):
        # 伪代码：确认上电安全状态 -> 配置两组 PWM -> 配置六路测速 -> 本地自检。
        # PWM 极性、频率、脉冲/转和最低占空比，均待实际电路验证后填写。
        raise NotImplementedError("Fan hardware driver is not implemented")

    def set_group_pwm(self, group, percent):
        # 伪代码：验证组号和范围 -> 按实际电路极性换算 duty -> 写入对应 PWM 通道。
        raise NotImplementedError("PWM output is not implemented")

    async def read(self):
        # 伪代码：按单调时钟统计六路脉冲 -> 原子读取计数 -> 换算每路 RPM -> 检测超时。
        # 不能根据 PWM 百分比反推实际转速；停转和未连接的区分也须实测。
        raise NotImplementedError("Fan tachometer is not implemented")


def create_hardware(settings=app_config):
    if settings.HARDWARE_MODE == "simulated":
        return (SimulatedSensor(settings.SIM_TEMPERATURE_C, settings.SIM_HUMIDITY_PERCENT),
                SimulatedFans(settings.FAN_GROUPS, settings.SIM_GROUP_PWM_PERCENT,
                              settings.SIM_RPM_AT_FULL_PWM))
    if settings.HARDWARE_MODE == "hardware":
        return HardwareSensor(), HardwareFans()
    raise ValueError("Unknown HARDWARE_MODE; no automatic simulation fallback")
