"""Paper 网关的合约规格内存状态与官方刷新委托。"""

from collections.abc import Callable

from ..gateway.base import Contract, ContractNotFound

ContractProvider = Callable[[str], Contract]


class PaperContractStateMixin:
    """集中管理模拟撮合使用的合约规格。"""

    _contracts: dict[str, Contract]
    _contract_provider: ContractProvider | None

    def upsert_contract(self, contract: Contract) -> None:
        """写入或覆盖内存中的合约规格。

        参数：
            contract: Contract，合约规格

        返回：
            None，就地更新合约规格表
        """
        self._contracts[contract.name] = contract

    def get_contract(self, contract: str) -> Contract:
        """按合约名读取当前内存规格。

        参数：
            contract: str，合约名

        返回：
            Contract：当前内存规格

        异常：
            ContractNotFound：合约不存在时抛出
        """
        if contract not in self._contracts:
            raise ContractNotFound(f"合约不存在: {contract}", label="CONTRACT_NOT_FOUND")
        return self._contracts[contract]

    def refresh_contract(self, contract: str) -> Contract:
        """从公共 Gate 提供方刷新规格并写入模拟撮合内存。

        参数：
            contract: str，合约名

        返回：
            Contract：最新规格；未注入公共提供方时返回当前内存规格
        """
        if self._contract_provider is None:
            return self.get_contract(contract)
        latest = self._contract_provider(contract)
        self.upsert_contract(latest)
        return latest
