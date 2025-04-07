from algopy import (
    Bytes,
    TemplateVar,
    TransactionType,
    Txn,
    UInt64,
    gtxn,
    logicsig,
    op,
    subroutine,
)
from algopy.arc4 import arc4_signature

TXN_FEE_ARG_POSITION = 5

@logicsig
def TSS() -> bool:
    """
       The treasury smart signature (TSS) is responsible for signinig and funding withdrawal transactions.
       It can sign an app call to withdraw funds from the main contract to a zero-balance address (mode 1).

       It can sign calls to the noop method of the main contract to increase the opcode budget,
       but will not pay the transaction fee for that (mode 2).
    """

    prevTxn = gtxn.Transaction(Txn.group_index - 1)
    currentTxn = gtxn.Transaction(Txn.group_index)

    # mode 1: make a withdrawal from the main contract
    # check that:
    # - previous transaction is a call to the main contract withdraw method
    # - current transaction is an app call to the main contract noop method
    # - txn fee is not higher than the txn fee requested to the main contract
    if is_app_call_to(prevTxn, arc4_signature("withdraw(byte[32][],byte[32][],account,bool,uint64)(uint64,byte[32])")):
        assert is_app_call_to(currentTxn, arc4_signature("noop(uint64)void")), "wrong method"
        assert currentTxn.fee <= op.btoi(prevTxn.app_args(TXN_FEE_ARG_POSITION)), "fee too high"
        return True

    # mode 2: sign noop transactions to increase the opcode budget
    # check that:
    # - current transaction is an app call to the main contract noop method
    # - the fee is zero
    if is_app_call_to(currentTxn, arc4_signature("noop(uint64)void")):
        assert currentTxn.fee == 0, "fee must be zero"
        return True

    assert False, "invalid mode"

@subroutine
def is_app_call_to(txn: gtxn.Transaction, methodSignature: Bytes) -> bool:
    """Check if the current transaction is an app call to the main contract given method."""
    return (
        txn.type == TransactionType.ApplicationCall
        and txn.app_id.id == TemplateVar[UInt64]("MAIN_CONTRACT_APP_ID")
        and txn.app_args(0) == methodSignature
    )