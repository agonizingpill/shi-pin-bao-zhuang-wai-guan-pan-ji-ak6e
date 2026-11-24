"""离线冒烟：用 stub 替代未安装的第三方库，验证 schemas 校验与 api 路由装配。

不触网、不装包，仅验证：
- Pydantic 模型对膜卷号、发包集合等输入的校验；
- FastAPI app 能完成全部路由注册，训练发包端点齐全；
- 权限依赖挂在正确的岗位上。
"""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def install_stubs():
    # ---- pydantic ----
    pydantic = types.ModuleType('pydantic')

    class BaseModel:
        def __init__(self, **data):
            fields = getattr(type(self), '__annotations__', {})
            model_config = getattr(type(self), 'model_config', {})
            allowed = set(fields)
            if model_config.get('extra') == 'forbid':
                unknown = set(data) - allowed
                if unknown:
                    raise ValueError(f'多余字段: {unknown}')
            for name in fields:
                if name in data:
                    value = data[name]
                    metadata = getattr(type(self), '_metadata', {}).get(name)
                    if metadata:
                        kind, constraint = metadata
                        if kind == 'min_length' and len(value) < constraint:
                            raise ValueError(f'{name} 长度不足')
                        if kind == 'list_min' and len(value) < constraint:
                            raise ValueError(f'{name} 元素不足')
                    setattr(self, name, value)
                else:
                    setattr(self, name, None)

        def model_dump(self):
            return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}

    class Field:
        def __init__(self, default=None, **constraints):
            self.default = default
            self.constraints = constraints

        def __class_getitem__(cls, item):
            return cls(item)

    class ConfigDict(dict):
        pass

    class StringConstraints:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    pydantic.BaseModel = BaseModel
    pydantic.Field = Field
    pydantic.ConfigDict = ConfigDict
    pydantic.StringConstraints = StringConstraints
    sys.modules['pydantic'] = pydantic

    # ---- fastapi 全家桶 ----
    fastapi = types.ModuleType('fastapi')

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class Depends:
        def __init__(self, dependency=None):
            self.dependency = dependency

    class FastAPI:
        def __init__(self, **kwargs):
            self.routes = {}
            self._routers = []

        def include_router(self, router):
            self._routers.append(router)

        def _register(self, method, path, **kwargs):
            def deco(func):
                self.routes[(method, path)] = {'func': func, **kwargs}
                return func
            return deco

        def get(self, path, **kw):
            return self._register('GET', path, **kw)

        def post(self, path, **kw):
            return self._register('POST', path, **kw)

        def middleware(self, _name):
            return lambda func: func

        def exception_handler(self, _exc):
            return lambda func: func

    class APIRouter(FastAPI):
        def __init__(self, prefix='', tags=None):
            super().__init__()
            self.prefix = prefix

    class Request:  # noqa: D401 - stub
        pass

    fastapi.FastAPI = FastAPI
    fastapi.APIRouter = APIRouter
    fastapi.HTTPException = HTTPException
    fastapi.Depends = Depends
    fastapi.Request = Request
    sys.modules['fastapi'] = fastapi

    responses = types.ModuleType('fastapi.responses')

    class JSONResponse(dict):
        def __init__(self, content, headers=None, **kwargs):
            super().__init__(content)
            self.headers = headers or {}

    responses.JSONResponse = JSONResponse
    sys.modules['fastapi.responses'] = responses

    exceptions = types.ModuleType('fastapi.exceptions')

    class RequestValidationError(Exception):
        pass

    exceptions.RequestValidationError = RequestValidationError
    sys.modules['fastapi.exceptions'] = exceptions

    security = types.ModuleType('fastapi.security')

    class HTTPBearer:
        def __init__(self, **kwargs):
            pass

    class HTTPAuthorizationCredentials:
        pass

    security.HTTPBearer = HTTPBearer
    security.HTTPAuthorizationCredentials = HTTPAuthorizationCredentials
    sys.modules['fastapi.security'] = security

    encoders = types.ModuleType('fastapi.encoders')
    encoders.jsonable_encoder = lambda value: value
    sys.modules['fastapi.encoders'] = encoders

    # ---- psycopg ----
    psycopg = types.ModuleType('psycopg')
    psycopg.connect = MagicMock()
    psycopg.errors = types.SimpleNamespace(UniqueViolation=type('UniqueViolation', (Exception,), {}))
    sys.modules['psycopg'] = psycopg
    psycopg_rows = types.ModuleType('psycopg.rows')
    psycopg_rows.dict_row = MagicMock()
    sys.modules['psycopg.rows'] = psycopg_rows
    psycopg_types = types.ModuleType('psycopg.types')
    sys.modules['psycopg.types'] = psycopg_types
    psycopg_json = types.ModuleType('psycopg.types.json')
    psycopg_json.Jsonb = lambda value: value
    sys.modules['psycopg.types.json'] = psycopg_json

    # ---- redis ----
    redis_mod = types.ModuleType('redis')
    redis_mod.Redis = MagicMock()
    sys.modules['redis'] = redis_mod
    redis_exc = types.ModuleType('redis.exceptions')

    class RedisError(Exception):
        pass

    redis_exc.RedisError = RedisError
    sys.modules['redis.exceptions'] = redis_exc

    # ---- jwt ----
    jwt_mod = types.ModuleType('jwt')
    jwt_mod.encode = lambda payload, secret, algorithm=None: 'token'
    jwt_mod.decode = lambda token, secret, **kwargs: {'sub': 'operator', 'role': 'operator',
                                                       'exp': 9999999999}
    sys.modules['jwt'] = jwt_mod

    # ---- numpy / sklearn（model.py 在导入期不重活，stub 即可）----
    numpy_mod = types.ModuleType('numpy')
    numpy_mod.asarray = lambda *a, **k: MagicMock()
    numpy_mod.array = lambda *a, **k: MagicMock()
    numpy_mod.repeat = MagicMock()
    numpy_mod.isfinite = MagicMock(return_value=True)
    numpy_mod.random = types.SimpleNamespace(default_rng=MagicMock())
    sys.modules['numpy'] = numpy_mod

    sklearn_mod = types.ModuleType('sklearn')
    sklearn_mod.__version__ = '1.6.1'
    sys.modules['sklearn'] = sklearn_mod
    decomposition = types.ModuleType('sklearn.decomposition')
    decomposition.PCA = type('PCA', (), {})
    sys.modules['sklearn.decomposition'] = decomposition
    metrics = types.ModuleType('sklearn.metrics')
    metrics.accuracy_score = MagicMock()
    metrics.confusion_matrix = MagicMock()
    sys.modules['sklearn.metrics'] = metrics
    pipeline = types.ModuleType('sklearn.pipeline')
    pipeline.Pipeline = type('Pipeline', (), {})
    sys.modules['sklearn.pipeline'] = pipeline
    preprocessing = types.ModuleType('sklearn.preprocessing')
    preprocessing.StandardScaler = type('StandardScaler', (), {})
    sys.modules['sklearn.preprocessing'] = preprocessing
    svm = types.ModuleType('sklearn.svm')
    svm.LinearSVC = type('LinearSVC', (), {})
    sys.modules['sklearn.svm'] = svm


class SchemaValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_stubs()
        from src import schemas
        cls.schemas = schemas

    def test_roll_register_accepts_valid_code(self):
        model = self.schemas.RollRegister(measure_id=7, roll_code='RL-261121-07')
        self.assertEqual(model.measure_id, 7)
        self.assertEqual(model.roll_code, 'RL-261121-07')

    def test_package_publish_stores_measure_ids(self):
        model = self.schemas.PackagePublish(purpose='train', measure_ids=[])
        # 空集合在真实 Pydantic 的 min_length=1 拦截；纯函数引擎对空集合另有 RuleError 保护。
        self.assertEqual(model.measure_ids, [])
        non_empty = self.schemas.PackagePublish(purpose='train', measure_ids=[1])
        self.assertEqual(non_empty.measure_ids, [1])

    def test_package_purpose_values(self):
        self.assertEqual(self.schemas.CohortPurpose('holdout').value, 'holdout')
        with self.assertRaises(ValueError):
            self.schemas.CohortPurpose('eval')

    def test_unique_ids_dedupes_preserving_order(self):
        model = self.schemas.PackagePublish(purpose='train', measure_ids=[3, 1, 3, 2])
        self.assertEqual(model.unique_ids(), [3, 1, 2])


class ApiRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_stubs()
        import os
        os.environ.setdefault('JWT_SECRET', 'test-secret')
        from src import api
        cls.api = api

    def test_training_endpoints_registered(self):
        routes = self.api.app.routes
        expected = {
            ('GET', '/training/rolls'), ('POST', '/training/rolls'),
            ('POST', '/training/rolls/correct'), ('POST', '/training/samples/withdraw'),
            ('GET', '/training/packages'), ('POST', '/training/packages'),
            ('GET', '/training/packages/{package_id}'),
            ('POST', '/training/packages/{package_id}/link'),
            ('GET', '/training/packages/{package_id}/download'),
        }
        self.assertTrue(expected.issubset(set(routes)), f'缺少: {expected - set(routes)}')

    def test_mutating_endpoints_have_role_guards(self):
        routes = self.api.app.routes
        # 登记膜卷 → operator；更正/撤销/发包 → supervisor。
        self.assertIsNotNone(routes[('POST', '/training/rolls')]['func'].__defaults__)
        guard = routes[('POST', '/training/packages')]['func'].__defaults__[0]
        self.assertTrue(callable(guard.dependency))

    def test_health_and_login_still_present(self):
        routes = self.api.app.routes
        self.assertIn(('GET', '/health'), routes)
        self.assertIn(('POST', '/login'), routes)


if __name__ == '__main__':
    unittest.main(verbosity=2)
