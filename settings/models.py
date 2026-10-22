# -*- coding: utf-8 -*-
# @Time    : 2022/10/18 2:59 下午
# @Author  : rabbit
# @File    : models.py
# @Software: PyCharm
import datetime
import json
import time

from sqlalchemy import String, Integer, Boolean, Column, DateTime, ForeignKey, Float, JSON, TEXT, Index, Numeric, UniqueConstraint, func
from sqlalchemy.orm import relationship
from settings.database import Base


# from settings.models import User
# 用户表单
class User(Base):
    __tablename__ = "tb_user"
    user_id = Column(Integer, primary_key=True, index=True, unique=True, autoincrement="auto")  # 用户id，作为主键
    name = Column(String(100))  # 姓名
    code = Column(String(100), index=True)  # 网络代号
    telephone = Column(String(11), index=True, unique=True)  # 手机电话
    email = Column(String(100), index=True, unique=True)
    password = Column(String(100), index=True)
    is_staff = Column(Boolean, default=False)  # 是否为超级管理员
    last_login_time = Column(DateTime, default=func.now())
    is_activate = Column(Boolean, default=True)  # 是否活跃
    register_time = Column(DateTime, default=func.now())  # 注册时间
    update_time = Column(DateTime, default=func.now())  # 更新时间
    plus_user_expire_time = Column(DateTime, default=func.now())  # 超级会员过期时间
    plus_use_buy = Column(Integer, default=0)  # gpt4模型使用
    draw_count = Column(Integer, default=0)  # gpt4模型使用
    regular_user_expire_time = Column(DateTime, default=func.now())  # 普通会员过期时间
    token = Column(Float)  # 每天的使用次数
    plus_user_count = Column(Integer, default=6)  # gpt4模型使用
    is_new = Column(Integer, default=1)  # 标识是否为新用户
    is_year = Column(Integer, default=1)  # 标识是否为年度用户，如果为2就是gpt4 plus会员
    is_quarter = Column(Integer, default=1)  # 标识是否为季度用户，1为季度， 2为1天体验, 3为gpt4plus会员 4为gpt普通会员
    regular_user = Column(Integer)  # 普通会员用户

    is_delete = Column(Boolean, default=False)  # 软删除字段
    token_daily = Column(Float, default=0)  # 每天的token
    input_token = Column(Float, default=0)  # 总输入token
    output_token = Column(Float, default=0)  # 总输出token
    discount = Column(Float, default=1)  # 折扣
    ask_num = Column(Integer, default=0)  # 折扣
    answer_num = Column(Integer, default=0)  # 折扣
    money_rate = Column(Float, default=1)  # 倍率调整
    phone_key_revoked = Column(Boolean, nullable=False, default=False, server_default="0")  # 生成过 sk- 后，手机号不能再当密钥

    def to_dict(self):
        now_time = datetime.datetime.fromtimestamp(time.time())
        # if self.is_year == 1 or self.is_quarter == 2:
        #     self.plus_user_count = "无限"
        #     self.token = "无限"
        # elif self.plus_user_expire_time > now_time and self.is_year == 1 and self.is_quarter != 0:
        #     self.token = "无限"
        # else:
        #     self.token = self.token + self.regular_user

        return {
            "user_id": self.user_id,
            "userName": self.name,
            "userCode": self.code,
            "token": self.token,
            "is_staff": self.is_staff,
            "plus_user_expire_time": self.plus_user_expire_time,
            "regular_user_expire_time": self.regular_user_expire_time,
            "plus_user_count": "无折扣" if self.discount == 1 else self.discount
        }


class UserApiKey(Base):
    """用户自行创建的 API 密钥。只有 is_active 且未删除用户的 key 能调用中转。"""
    __tablename__ = "tb_user_api_key"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("tb_user.user_id", ondelete="CASCADE"), index=True, nullable=False)
    api_key = Column(String(80), unique=True, index=True, nullable=False)
    name = Column(String(40), nullable=False, default="")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())


# 标签表单
class Tag(Base):
    __tablename__ = "tb_tag"
    tag_id = Column(Integer, primary_key=True, index=True, autoincrement="auto")
    tag_name = Column(String(50))  # 标签名称
    level = Column(Integer)  # 标签等级 一级 二级 等等
    insert_time = Column(DateTime, default=func.now())  # 插入时间
    is_delete = Column(Boolean, default=False)  # 软删除


# 提示词分类表单
class TagPrompt(Base):
    __tablename__ = "tb_prompt_tag"
    tag_id = Column(Integer, ForeignKey("tb_tag.tag_id", ondelete="CASCADE"), index=True, primary_key=True)  # 用户id
    prompt_id = Column(Integer, ForeignKey("tb_prompt.prompt_id", ondelete="CASCADE"),
                       index=True, primary_key=True)  # 联合的功能点id
    insert_time = Column(DateTime, default=func.now())  # 插入时间
    update_time = Column(DateTime, default=func.now())  # 更新时间
    is_delete = Column(Boolean, default=False)  # 软删除


# # 对话表单，利用时间戳为单位
# class Chat(Base):
#     __tablename__ = "tb_chat"  # 数据库中表的名字
#     chat_id = Column(Integer, index=True, primary_key=True, autoincrement=True)
#     user_id = Column(Integer, ForeignKey("tb_user.user_id", ondelete="CASCADE"), index=True)
#     chat_list = Column(JSON, default="")
#     update_time = Column(DateTime, default=func.now())  # 注册时间
#     is_delete = Column(Boolean, default=False)  # 软删除  # 一对一关系限定

# 功能记录表
class FunctionCode(Base):
    __tablename__ = "tb_function"
    function_id = Column(Integer, primary_key=True, index=True, autoincrement="auto")
    function_code = Column(String(50), index=True, unique=True)
    insert_time = Column(DateTime, default=func.now())  # 插入时间
    update_time = Column(DateTime, default=func.now())  # 更新时间
    function_name = Column(String(50), index=True, unique=True)  # 功能中文名


# 用户行为记录表
class UserActionRecord(Base):
    __tablename__ = "tb_user_action_record"
    user_id = Column(Integer, ForeignKey("tb_user.user_id", ondelete="CASCADE"), index=True, primary_key=True)  # 用户id
    function_id = Column(Integer, ForeignKey("tb_function.function_id", ondelete="CASCADE"),
                         index=True, primary_key=True)  # 联合的功能点id
    use_num = Column(Integer, default=0)  # 使用次数
    update_time = Column(DateTime, default=func.now())  # 更新时间


# prompt表单
class Prompt(Base):
    __tablename__ = "tb_prompt"
    prompt_id = Column(Integer, primary_key=True, index=True, autoincrement="auto")
    prompt_text = Column(TEXT)
    insert_time = Column(DateTime, default=func.now())
    insert_by = Column(Integer, ForeignKey("tb_user.user_id", ondelete="CASCADE"), index=True)
    update_time = Column(DateTime, default=func.now())
    update_by = Column(Integer, ForeignKey("tb_user.user_id", ondelete="CASCADE"), index=True)
    is_delete = Column(Boolean, default=False)  # 软删除
    index_prompt_text = Index('p_text', 'prompt_text', mysql_length=255)


# prompt使用表
class PromptUse(Base):
    __tablename__ = "tb_prompt_use"
    prompt_id = Column(Integer, ForeignKey("tb_prompt.prompt_id", ondelete="CASCADE"), index=True, primary_key=True)
    used_by = Column(Integer, ForeignKey("tb_user.user_id", ondelete="CASCADE"), index=True, primary_key=True)
    insert_time = Column(DateTime)
    update_time = Column(DateTime)
    is_delete = Column(Boolean, default=False)
    use_num = Column(Integer)


class UserContext(Base):
    __tablename__ = "tb_user_context"
    context_id = Column(String(13), index=True, primary_key=True, default=str(int(time.time() * 1000)))
    context_uuid = Column(String(100), index=True, default=None)
    used_by = Column(Integer, ForeignKey("tb_user.user_id", ondelete="CASCADE"), index=True)
    insert_time = Column(DateTime, default=func.now())
    update_time = Column(DateTime, default=func.now())
    is_delete = Column(Boolean, default=False)
    context = Column(JSON, default=json.dumps([]))  # 该字段记录用户对话
    current_id_gpt = Column(String(100), index=True, default=None)
    auth = Column(String(100), index=True, default=None)
    parent_id = Column(String(5000), index=True, default=None)


class PricingSettings(Base):
    """Singleton global pricing settings; row id is always 1."""
    __tablename__ = "tb_pricing_settings"
    id = Column(Integer, primary_key=True)
    usd_cny_rate = Column(Float, nullable=False, default=6.9)
    profit_multiplier = Column(Float, nullable=False, default=1.5)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    updated_by = Column(Integer, ForeignKey("tb_user.user_id", ondelete="SET NULL"), nullable=True)


class UpstreamVendor(Base):
    """上游供应商：一个 base_url 对应一套模型价目。

    同一 base_url 可挂多个 code（如 yunxin-02 / yunxin-03），用不同 cost_rate 区分计价组。
    pricing_mode:
      - manual：手工维护 tb_vendor_model_price，或走 pricing_url 同步
      - catalog_rate：成本价 = OpenAI 官方标准目录 × cost_rate，再走全局加价/用户倍率扣费
    """
    __tablename__ = "tb_upstream_vendor"
    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(64), nullable=False, unique=True, index=True)
    name = Column(String(128), nullable=False, default="")
    base_url = Column(String(512), nullable=False, index=True)
    api_key = Column(String(512), nullable=True)  # 转发到该号商时使用的上游密钥
    # 存量 api_key 是否已迁到 tb_upstream_vendor_key；迁过就不再自动重建，避免删掉后被重新插入
    key_migration_done = Column(Boolean, nullable=False, default=False, server_default="0")
    pricing_url = Column(String(512), nullable=True)
    pricing_mode = Column(String(32), nullable=False, default="manual")  # manual | catalog_rate
    cost_rate = Column(Float, nullable=True)  # e.g. 0.2；仅 catalog_rate 使用
    # 按原始供应商控制展示和倍率。例：{"providers":{"anthropic":{"enabled":true,"cost_rate":0.3},"xai":{"enabled":false}}}
    catalog_policy = Column(JSON, nullable=True)
    profit_multiplier = Column(Float, nullable=True)  # 空则用全局 tb_pricing_settings.profit_multiplier
    is_active = Column(Boolean, nullable=False, default=True)
    is_default = Column(Boolean, nullable=False, default=False)
    sort_order = Column(Integer, nullable=False, default=100)
    last_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class UpstreamVendorKey(Base):
    """供应商下的多把命名密钥。

    一个 vendor 可以挂多把 Key（如 tongtong 的 4 把），模型路由直接选其中一把；
    换 Key 只改这里，不用重建路由。vendor.api_key 保留为最终兜底。
    """
    __tablename__ = "tb_upstream_vendor_key"
    id = Column(Integer, primary_key=True, autoincrement=True)
    vendor_id = Column(Integer, ForeignKey("tb_upstream_vendor.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(128), nullable=False, default="")
    api_key = Column(String(512), nullable=False)
    is_default = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    note = Column(String(200), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        UniqueConstraint("vendor_id", "name", name="uq_vendor_key_name"),
    )


class ModelRoute(Base):
    """按模型覆盖上游。默认走当前 is_default 供应商，命中规则则改走指定 vendor。

    user_ids 为空表示全部用户；非空时只有这些用户改走该厂商，计价也用该厂商价目。
    api_key_id 指向 tb_upstream_vendor_key，选择该 vendor 下的某把命名 Key。
    """
    __tablename__ = "tb_model_route"
    id = Column(Integer, primary_key=True, autoincrement=True)
    model = Column(String(255), nullable=False, unique=True, index=True)
    vendor_code = Column(String(64), nullable=False, index=True)
    api_key = Column(String(512), nullable=True)  # 命中该模型时覆盖供应商密钥；空则沿用供应商
    api_key_id = Column(Integer, nullable=True)  # 选指定 vendor Key；与 api_key 互斥
    user_ids = Column(JSON, nullable=True)  # 空/NULL=全部用户；否则仅这些 tb_user.user_id
    is_active = Column(Boolean, nullable=False, default=True)
    note = Column(String(200), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class ModelProtocol(Base):
    """按模型指定调用协议。网页和 /v1/models 按这里下发，避免客户端选错协议。

    model 支持精确名、以 - 为边界的前缀，以及 gemini* 这种开头通配。
    """
    __tablename__ = "tb_model_protocol"
    id = Column(Integer, primary_key=True, autoincrement=True)
    model = Column(String(255), nullable=False, unique=True, index=True)
    protocol = Column(String(32), nullable=False)  # openai | anthropic | responses
    is_active = Column(Boolean, nullable=False, default=True)
    note = Column(String(200), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class PromptInjection(Base):
    """中转站系统提示注入。scope=all 与类别/指定模型规则可以同时命中。"""
    __tablename__ = "tb_prompt_injection"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(80), nullable=False, default="")
    scope = Column(String(16), nullable=False, default="all")  # all | category | model
    target = Column(String(255), nullable=False, default="")
    content = Column(TEXT, nullable=False)
    position = Column(String(16), nullable=False, default="append")  # append | prepend | first
    is_active = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=100)
    note = Column(String(200), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class ModelRewrite(Base):
    """请求里的旧模型改写成新模型后再转发。

    用量记录、扣费和计费展示都用改写后的模型。
    model 支持精确名、以 - 为边界的前缀，以及 gemini* 这种开头通配。
    user_ids 为空表示全部用户。
    """
    __tablename__ = "tb_model_rewrite"
    id = Column(Integer, primary_key=True, autoincrement=True)
    model = Column(String(255), nullable=False, unique=True, index=True)
    target_model = Column(String(255), nullable=False, index=True)
    user_ids = Column(JSON, nullable=True)  # 空/NULL=全部用户；否则仅这些 tb_user.user_id
    is_active = Column(Boolean, nullable=False, default=True)
    note = Column(String(200), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class ModelAlias(Base):
    """对外模型别名。列表展示 alias，请求时还原成 target_model 再转发/计价。"""
    __tablename__ = "tb_model_alias"
    id = Column(Integer, primary_key=True, autoincrement=True)
    alias = Column(String(255), nullable=False, unique=True, index=True)
    target_model = Column(String(255), nullable=False, index=True)
    protocol = Column(String(32), nullable=False, default="auto")  # auto | openai | anthropic | responses
    hide_target = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    note = Column(String(200), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class VendorModelPrice(Base):
    """供应商维度的模型价格（USD / 1M tokens）。"""
    __tablename__ = "tb_vendor_model_price"
    id = Column(Integer, primary_key=True, autoincrement=True)
    vendor_id = Column(Integer, ForeignKey("tb_upstream_vendor.id", ondelete="CASCADE"), nullable=False, index=True)
    model = Column(String(255), nullable=False, index=True)
    service_tier = Column(String(32), nullable=False, default="standard")
    kind = Column(String(32), nullable=False, default="chat")  # chat | image | embedding
    display_name = Column(String(255), nullable=False, default="")
    owned_by = Column(String(64), nullable=False, default="")
    input_price = Column(Float, nullable=False, default=0)
    output_price = Column(Float, nullable=False, default=0)
    cache_read_price = Column(Float, nullable=False, default=0)
    cache_write_price = Column(Float, nullable=False, default=0)
    text_input_price = Column(Float, nullable=False, default=0)
    image_input_price = Column(Float, nullable=False, default=0)
    # 可选原价（USD / 1M tokens）。设置后 1:1 按原价计美元，人民币扣费只乘汇率；
    # 上方 input/output 等仍作上游成本价，用于 cost_cny / 利润。
    original_input_price = Column(Float, nullable=True)
    original_output_price = Column(Float, nullable=True)
    original_cache_read_price = Column(Float, nullable=True)
    original_cache_write_price = Column(Float, nullable=True)
    original_text_input_price = Column(Float, nullable=True)
    original_image_input_price = Column(Float, nullable=True)
    currency = Column(String(8), nullable=False, default="USD")
    unit = Column(String(32), nullable=False, default="per_1m_tokens")
    is_active = Column(Boolean, nullable=False, default=True)
    raw_payload = Column(JSON, nullable=True)
    source_updated_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        UniqueConstraint("vendor_id", "model", "service_tier", name="uq_vendor_model_tier"),
        Index("ix_vendor_model_price_vendor_model", "vendor_id", "model"),
    )


class RequestUsage(Base):
    __tablename__ = "tb_request_usage"
    request_id = Column(String(32), primary_key=True)
    user_id = Column(Integer, ForeignKey("tb_user.user_id", ondelete="SET NULL"), nullable=True, index=True)
    model = Column(String(255), nullable=False, default="")
    path = Column(String(255), nullable=False)
    source = Column(String(32), nullable=False, default="")
    status = Column(String(24), nullable=False, default="pending")
    http_status = Column(Integer, nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    cache_read_tokens = Column(Integer, nullable=True)
    cache_write_tokens = Column(Integer, nullable=True)
    # short / long；没有长短上下文差价的模型为空
    context_tier = Column(String(16), nullable=True)
    charged_cny = Column(Numeric(18, 8), nullable=True)
    # 这笔扣费之后的账户余额。未计费或更早的记录为空。
    balance_cny = Column(Numeric(18, 8), nullable=True)
    cost_cny = Column(Numeric(18, 8), nullable=True)
    profit_cny = Column(Numeric(18, 8), nullable=True)
    ttfb_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        Index("ix_request_usage_user_created", "user_id", "created_at"),
        Index("ix_request_usage_created_status", "created_at", "status"),
        Index("ix_request_usage_request_id", "request_id"),
    )


# 账单表
class Bill(Base):
    __tablename__ = "tb_bill"
    bill_id = Column(String(50), primary_key=True, index=True, autoincrement="auto")
    generate_time = Column(DateTime, default=func.now())
    generate_by = Column(Integer, ForeignKey("tb_user.user_id", ondelete="CASCADE"), index=True)
    money = Column(Float)
    function_id = Column(Integer, ForeignKey("tb_function.function_id", ondelete="CASCADE"),
                         index=True)  # 联合的功能点id
    is_delete = Column(Boolean, default=False)
    delete_time = Column(DateTime, default=None)


# 保密人员名单
class TeacherSecret(Base):
    __tablename__ = "tb_teacher_secret"
    id = Column(Integer, primary_key=True, autoincrement="auto", index=True)
    user_id = Column(Integer, ForeignKey("tb_user.user_id", ondelete="CASCADE"),
                     index=True)  # 联合的功能点id


class AgentTask(Base):
    """中转侧的代理任务。状态由访问日志的 finish / result 归并，也可以手动改。"""
    __tablename__ = "tb_agent_task"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("tb_user.user_id", ondelete="SET NULL"), nullable=True, index=True)
    account = Column(String(32), nullable=False, default="", index=True)
    session_key = Column(String(200), nullable=False, default="")
    model = Column(String(255), nullable=False, default="")
    source = Column(String(32), nullable=False, default="")
    path = Column(String(255), nullable=False, default="")
    status = Column(String(16), nullable=False, default="running", index=True)
    status_source = Column(String(16), nullable=False, default="log")
    suggested_status = Column(String(16), nullable=False, default="running")
    finish = Column(String(64), nullable=False, default="")
    result = Column(String(64), nullable=False, default="")
    decision = Column(String(300), nullable=False, default="")
    steps = Column(Integer, nullable=False, default=1)
    last_request_id = Column(String(32), nullable=False, default="")
    request_ids = Column(String(400), nullable=False, default="")
    http_status = Column(Integer, nullable=True)
    started_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now())
    is_delete = Column(Boolean, nullable=False, default=False)
    __table_args__ = (
        Index("ix_agent_task_account_status", "account", "is_delete", "status"),
        Index("ix_agent_task_user_status", "user_id", "is_delete", "status"),
    )
