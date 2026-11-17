import jwt
from fastapi import HTTPException, status

from customer_service.app.schemas.user import CurrentUser
from customer_service.common.config import get_settings


class AuthService:
    """
    认证服务
    """

    def __init__(self):
        settings = get_settings()
        self.secret_key = settings.jwt_secret
        self.algorithm = settings.jwt_algorithm

    def get_authorized_user(
        self,
        authorization: str | None,
        *role: str,
    ) -> CurrentUser:
        """
        职责：获取当前用户信息以及完成角色的校验
        :param authorization: 用户的授权信息
        :param role: 用户的角色列表
        :return: 当前用户信息
        """

        # 1. 根据令牌获取用户信息
        current_user = self._get_current_user(authorization)

        # 2. 校验用户角色
        if current_user.role not in role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="用户没有权限"
            )

        return current_user

    def _get_current_user(self, authorization: str | None) -> CurrentUser:
        """
        职责：根据授权信息获取当前用户
        :param authorization: 用户的授权信息
        :return: 当前用户信息
        """

        # 1. 获取令牌
        access_token = self._extract_token(authorization)

        # 2. 根据令牌获取用户信息
        current_user = self._decode_access_token(access_token)

        # 临时返回一个示例用户
        return current_user

    def _extract_token(self, authorization: str | None) -> str:
        """
        职责：从授权信息中提取令牌
        :param authorization: 用户的授权信息
        :return: 访问令牌
        """

        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="未提供授权信息"
            )
        try:
            return authorization.split(" ")[1]
        except IndexError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的授权信息"
            )

    def _decode_access_token(self, access_token: str) -> CurrentUser:
        """
        职责：根据访问令牌解码获取当前用户信息
        :param access_token: 干净的令牌信息
        :return: 当前用户信息
        """

        payload = jwt.decode(
            access_token,
            self.secret_key,
            algorithms=[self.algorithm],
        )

        return CurrentUser.model_validate(payload)
