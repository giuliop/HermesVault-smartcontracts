# Protocol overview

An address can make deposits of algo tokens in any amount to the application
contract, keeping a secret receipt. The address and the deposited amount are
public on the blockchain.

Then, with the secret receipt, any address can withdraw part, or all, of those
tokens or send them to another address. The receiving address and the withdrawn
amount will be public, but the source of the withdrawal, that is the original
deposited amount and the original depositor address, will remain private.

Moreover, the withdrawal transaction will be signed by a smart signature provided
by the protocol, so the receiving address can be a zero balance account
(e.g., a new account with no history) since the smart signature will pay the
transaction fees from the withdrawed amount.

For each withdrawal, the application will create a new deposit with the "change"
amount (the difference between the original deposit and the withdrawal) to be used
for future withdrawals with the same privacy guarantees. Even if the change is
zero, a new "zero" amount deposit will be created to avoid leaking information.


# Implementation

There are two zk-circuits, `deposit_circuit` and `verifier_circuit`, described below.

The protocol is implemented onchain by the following components:
  - a main smart contract (***APP***) with the application logic
  - a deposit verifier smart signature (***DV***) to validate deposits' zk-proofs
  - a withdrawal verifier smart signature (***WV***) to validate withdrawals' zk-proofs
  - a treasury smart signature (***TSS***) to sign withdrawal transaction

The deposit and change receipts are stored in a merkle tree managed by ***APP***

Users can make deposits and withdrawals as described below.

## Deposit
The user generates random K,R and computes  
`Commitment = hash(hash(Amount, K, R))`

The user generates a proof using `deposit_circuit` with
```
Public inputs:  Amount
                Commitment

Private inputs: K
                R
```
The proof proves that  
```
Commitment = hash(hash(Amount,K,R))
```

The user sends a transaction group with two transactions:
1. An app call to ***APP***'s deposit method signed by ***DV*** with args `(proof, public inputs, sender)`
2. A payment of `Amount` tokens to ***APP*** from `sender`

If the proof is invalid the ***DV*** will reject and the transaction group fail

***APP*** verifies that
* The calling transaction is signed by ***DV***
* The next group transaction is a payment of `Amount` to ***APP*** from `sender`
* Amount is at least `1 algo` (minimum deposit requirement)

If all is verified, ***APP*** will inserts the deposit in the merkle tree

## Withdraw
The user generates random `K2`,`R2` and a proof using `withdraw_circuit` with
```
Public inputs:  Recipient  -> address for withdrawal
                Withdrawal -> amount for withdrawal
                Fee        -> amount for protocol expenses
                Commitment -> hash(Change, K2, R2)
                Nullifier  -> hash(Amount, K)
                Root       -> of merkle tree

Private inputs: K          -> old deposit secret K
                R          -> old deposit secret R
                Amount     -> old deposit amount
                Change     -> change tokens: Amount - Withdrawal - Fee
                K2         -> change (new deposit) secret value
                R2         -> change (new deposit) secret value
                Index      -> of (Amount,K,R) in merkle tree (old deposit)
                Path       -> Merkle proof for (Amount, K, R) at Index with Root
```

The merkle Path starts with value `(Amount, K,R)` not hashed, then its
sibling, then all the parent nodes' siblings up to, but excluding the root.

The proof proves that:
```
Nullifier  == hash(Amount, K)
Commitment == hash(hash(Change, K2, R2))
Change     == Amount - Withdrawal - Fee
Change, Amount, Withdrawal, Fee are all >= 0
Amount,K are the initial part of Path[0] (which is Amount,K,R)
Path is a valid merkle proof for value (Amount,K,R) at Index with Root
```

The user sends a transaction group with two transactions:
1. An app call to ***APP***'s withdraw method signed by ***WV*** with args (proof, public inputs, recipient, no_change, extra_txn_fee)
2. An (optional) app call to ***APP***'s noop method signed by the ***TSS*** to cover the transaction fees

If the proof is invalid the ***DV*** will reject and the transaction fail

***APP*** verifies that
1. `Nullifier` has not been previously spent
2. `Root` is a valid root
3. `Fee` is at least `max(10^5, 0.1% * Withdraw)` (10^5 microalgo == 0.1 algo)

If all is verified, ***APP***
- adds `Nullifier` to the list of used nullifiers
- sends `Withdrawal` tokens to the `Recipient` minus optional extra network fees
  as described below
- sends `Fee` tokens to ***TSS*** plus optional extra network fees
- inserts `Change` in the merkle tree

***TSS*** (if invoked) verifies that
1. the previous transaction in the group is a call to ***APP***'s withdraw method as per above
2. the current transaction is an app call to ***APP***'s noop method
3. the transaction fee is not higher than what is required to pay all fees

If the user wants the ***TSS*** to pay for more than minimum fees to the Algorand
network (e.g., for congestion), it can specify that to the Contract and the
extra fee will be subtracted to the Withdrawal amount.

After a successful withdrawal, the user is now able to withdraw `Change` in the
future using the new `Commitment` 

If the tree is full, the user can withdraw but no new change can be inserted.
So if the user withdraws less than the total amount, the change is locked
forever in the contract.
To make sure the user is aware that the tree is full, the parameter `no-change` has to be
set to true.
