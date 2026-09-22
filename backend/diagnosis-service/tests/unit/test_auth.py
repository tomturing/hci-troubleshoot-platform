"""内部服务身份验证测试。"""

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import app.auth as auth_module
import pytest
from app.auth import CUSTOMER_ROLE, InternalTokenIdentityVerifier, OidcJwtIdentityVerifier
from app.errors import DiagnosisError
from app.services.collection_plan_service import PLAN_ROLES
from app.services.collector_artifact_service import ARTIFACT_ROLES
from app.services.diagnosis_session_service import CREATE_ROLES
from app.services.evidence_upload_service import UPLOAD_ROLES
from app.services.offline_analysis_service import REPORT_READ_ROLES
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def make_request(headers: dict[str, str]):
    """构造最小请求对象。"""

    return SimpleNamespace(headers=headers)


@pytest.mark.asyncio
async def test_internal_token_builds_fixed_privileged_actor():
    """直连内部调用方（无角色头）沿用固定内部角色。"""

    verifier = InternalTokenIdentityVerifier("secret-token")
    actor = await verifier.verify(
        make_request(
            {
                "Authorization": "Bearer secret-token",
                "X-Tenant-ID": "tenant-a",
                "X-Actor-ID": "api-gateway",
            }
        )
    )

    assert actor.tenant_id == "tenant-a"
    assert actor.user_id == "api-gateway"
    assert actor.roles == frozenset({"platform_admin", "support_engineer", "diagnosis_worker"})


@pytest.mark.asyncio
async def test_unsupported_actor_roles_header_is_rejected():
    """非受支持角色头一律拒绝，不得静默升级为内部管理员。"""

    verifier = InternalTokenIdentityVerifier("secret-token")
    with pytest.raises(DiagnosisError) as exc_info:
        await verifier.verify(
            make_request(
                {
                    "Authorization": "Bearer secret-token",
                    "X-Tenant-ID": "tenant-a",
                    "X-Actor-ID": "api-gateway",
                    "X-Actor-Roles": "superuser",
                }
            )
        )

    assert exc_info.value.code == "INVALID_ACTOR_CONTEXT"
    assert exc_info.value.http_status == 422


@pytest.mark.asyncio
async def test_supplied_internal_roles_are_limited_to_allowlist():
    """管理员角色头只能落在内部角色白名单内，不得包含白名单外角色。"""

    verifier = InternalTokenIdentityVerifier("secret-token")
    actor = await verifier.verify(
        make_request(
            {
                "Authorization": "Bearer secret-token",
                "X-Tenant-ID": "tenant-a",
                "X-Actor-ID": "gateway-admin",
                "X-Actor-Roles": "support_engineer, superuser",
            }
        )
    )

    assert actor.roles == frozenset({"support_engineer"})


@pytest.mark.asyncio
async def test_customer_role_requires_unforgeable_owner_key():
    """客户角色必须携带网关签发的归属键，缺失一律拒绝。"""

    verifier = InternalTokenIdentityVerifier("secret-token")
    actor = await verifier.verify(
        make_request(
            {
                "Authorization": "Bearer secret-token",
                "X-Tenant-ID": "default",
                "X-Actor-ID": "cust-1a2b3c",
                "X-Actor-Roles": "customer",
                "X-Actor-Customer-ID": "1a2b3c",
            }
        )
    )

    assert actor.roles == frozenset({"customer"})
    assert actor.customer_id == "1a2b3c"

    with pytest.raises(DiagnosisError) as exc_info:
        await verifier.verify(
            make_request(
                {
                    "Authorization": "Bearer secret-token",
                    "X-Tenant-ID": "default",
                    "X-Actor-ID": "cust-1a2b3c",
                    "X-Actor-Roles": "customer",
                }
            )
        )
    assert exc_info.value.code == "INVALID_ACTOR_CONTEXT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "expected_code"),
    [
        ({"X-Tenant-ID": "tenant-a"}, "UNAUTHORIZED"),
        (
            {"Authorization": "Bearer wrong", "X-Tenant-ID": "tenant-a"},
            "FORBIDDEN",
        ),
        ({"Authorization": "Bearer secret-token"}, "INVALID_TENANT_CONTEXT"),
    ],
)
async def test_internal_token_rejects_incomplete_or_invalid_identity(headers, expected_code):
    """缺失令牌、错误令牌和缺失租户均默认拒绝。"""

    verifier = InternalTokenIdentityVerifier("secret-token")
    with pytest.raises(DiagnosisError) as exc_info:
        await verifier.verify(make_request(headers))

    assert exc_info.value.code == expected_code


@pytest.mark.asyncio
async def test_oidc_verifier_uses_signed_claims_and_rejects_tamper():
    """正式身份只来自签名 JWT Claim，忽略浏览器伪造身份头。"""

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    verifier = OidcJwtIdentityVerifier(
        public_key_pem_base64=base64.b64encode(public_pem).decode(),
        issuer="https://identity.example.com",
        audience="hci-diagnosis",
    )
    header = _base64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _base64url(
        json.dumps(
            {
                "iss": "https://identity.example.com",
                "aud": "hci-diagnosis",
                "sub": "user-1",
                "tenant_id": "tenant-signed",
                "customer_id": "customer-1",
                "roles": ["customer_admin"],
                "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            }
        ).encode()
    )
    signature = private_key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    token = f"{header}.{payload}.{_base64url(signature)}"

    actor = await verifier.verify(
        make_request(
            {
                "Authorization": f"Bearer {token}",
                "X-Tenant-ID": "attacker-tenant",
                "X-Actor-Roles": "platform_admin",
            }
        )
    )

    assert actor.tenant_id == "tenant-signed"
    assert actor.customer_id == "customer-1"
    assert actor.roles == frozenset({"customer_admin"})

    tampered = f"{header}.{payload[:-1]}A.{_base64url(signature)}"
    with pytest.raises(DiagnosisError) as exc_info:
        await verifier.verify(make_request({"Authorization": f"Bearer {tampered}"}))
    assert exc_info.value.code == "INVALID_IDENTITY_TOKEN"


@pytest.mark.asyncio
async def test_oidc_verifier_resolves_rotated_key_from_https_jwks(monkeypatch):
    """JWKS 模式必须按 kid 获取轮换公钥，且不依赖固定 PEM。"""

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    jwk = {
        "kid": "rotated-2026-08",
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "n": _base64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _base64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"keys": [jwk]}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", FakeClient)
    verifier = OidcJwtIdentityVerifier(
        jwks_url="https://identity.example.com/.well-known/jwks.json",
        issuer="https://identity.example.com",
        audience="hci-diagnosis",
    )
    header = _base64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": jwk["kid"]}).encode())
    payload = _base64url(
        json.dumps(
            {
                "iss": "https://identity.example.com",
                "aud": "hci-diagnosis",
                "sub": "user-rotated",
                "tenant_id": "tenant-signed",
                "roles": ["customer_admin"],
                "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            }
        ).encode()
    )
    signature = private_key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())

    actor = await verifier.verify(
        make_request({"Authorization": f"Bearer {header}.{payload}.{_base64url(signature)}"})
    )

    assert actor.user_id == "user-rotated"
    assert actor.tenant_id == "tenant-signed"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def test_customer_role_admitted_by_customer_side_permissions():
    """浏览器客户角色必须被客户侧权限集合接受。

    B1 把经网关身份 Cookie 认证的浏览器客户统一降级为 `customer` 角色。若客户侧
    权限集合漏掉该角色，客户离线诊断（读场景、创建会话、上传证据、读报告）会
    全部 403——这是 B1 合入后实测到的回归，此处固化为契约。
    """

    assert CUSTOMER_ROLE in CREATE_ROLES
    assert CUSTOMER_ROLE in UPLOAD_ROLES
    assert CUSTOMER_ROLE in REPORT_READ_ROLES


def test_customer_role_excluded_from_management_permissions():
    """同一角色不得进入管理面权限集合，否则降级形同虚设。"""

    assert CUSTOMER_ROLE not in ARTIFACT_ROLES
    assert CUSTOMER_ROLE not in PLAN_ROLES
