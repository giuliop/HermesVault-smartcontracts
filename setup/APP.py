import typing

import algopy as py
from algopy import (
    Account,
    Bytes,
    Global,
    TemplateVar,
    Txn,
    UInt64,
    itxn,
    op,
    subroutine,
    urange,
)
from algopy.arc4 import Address, Bool, Byte, DynamicArray, StaticArray, abimethod

Bytes32: typing.TypeAlias = StaticArray[Byte, typing.Literal[32]]

curve_mod = 21888242871839275222246405745257275088548364400416034343698204186575808495617

deposit_minimum_amount = 1_000_000 # 1 Algo
withdrawal_fee_divisor = 1_000 # 0.1% (we divide by this to get the fee)
withdrawal_minimum_fee = 100_000 # 0.1 Algo

# Depth of the Merkle tree to store the commitments, not counting the root.
# The leaves are at depth 0 and there are 2**tree_depth leaves.
# The tree is inizialized with the hash of 0 for all leaves
tree_depth = 24
max_leaves = 16_777_216

deposit_opcode_budget_opup = 27_300
withdrawal_opcode_budget_opup = 30_100

# How many last roots to store so that concurrent verifiers can check their
# proof without having their root overwritten by other transactions
roots_count = 50

# The variable in  global storage are:
# manager               -> address of the manager for the limited admin functions
# immutable             -> initially false, a flag to make the contract immutable
# initialized           -> initially false, will be set to true after initialization
# TSS                   -> treasury smart signature address
# inserted_leaves_count -> number of leaves inserted in the tree
# root                  -> current root hash
# next_root_index       -> index of the next root to add, between 0,roots_count

# In box storage we have (key -> value):
# b'roots'              -> 32*roots_count bytes
# b'subtree'            -> 32*(tree_depth) bytes (see below)
# <32_byte_nullifier>   -> if it exists, nullifier was spent

# In 'subtree' we store a compact representation of the merkle tree: path from
# last inserted leaf to root (excluded), enough to recompute the root on insertions

# Note that the app needs to be prefunded with MBR for roots and subtree boxes (e.g.,
# with 24 tree depth and 50 roots, 2500 + 400*(5 + 32*50) = 644,500 microalgo for roots
# and 2500 + 400 * (7 * 32*24) = 312,500 microalgo for the subtree)
# The `init` method will create the boxes for the roots and subtree and is meant to be
# called after the contract is funded.`

# how much the app minimum balance must be increased for each nullifier box in microalgo
# 2500 + 400 * 32 = 15_300
nullifier_MBR = 15_300

class APP(py.ARC4Contract, avm_version=11):
    @abimethod(create='require')
    def create(self, manager: Account) -> None:
        """Create the application"""
        self.manager = manager
        self.immutable = False
        self.initialized = False

        # TSS (treasury smart signature) address will be added after creation
        # since we need the main contract application id to create the TSS
        self.TSS = Global.zero_address

        self.inserted_leaves_count = UInt64(0)
        self.root = Bytes32.from_bytes(b'')
        self.next_root_index = UInt64(0)

    @abimethod
    def init(self, tss: Account) -> None:
        """Initialize the application (creator only).
           Call after creation and funding to create boxes and set the TSS address.
           Once initialized, the contract cannot be re-initialized."""
        assert Txn.sender == Global.creator_address
        assert not self.initialized
        op.Box.create(b'roots', 32*roots_count)
        op.Box.create(b'subtree', 32*tree_depth)
        self.update_tree_with(Bytes32.from_bytes(b''))
        self.TSS = tss
        self.initialized = True

    @abimethod
    def set_TSS(self, tss: Account) -> None:
        """Set the treasury smart signature address, if the application is still
           mutable (manager only)"""
        assert not self.immutable
        assert Txn.sender == self.manager
        self.TSS = tss

    @abimethod(allow_actions=["UpdateApplication", "DeleteApplication"])
    def update(self) -> None:
        """Update the application if it is mutable (manager only)"""
        assert Txn.sender == self.manager
        assert not self.immutable

    @abimethod
    def set_immutable(self) -> None:
        """Set the contract as immutable (manager/creator only)"""
        assert Txn.sender == self.manager or Txn.sender == Global.creator_address
        self.immutable = True

    @abimethod
    def validate_manager(self) -> None:
        """Fail if the sender is not the protocol manager.
           To be used by the TSS to allow manager-only functions"""
        assert Txn.sender == self.manager

    @abimethod
    def noop(self, counter: UInt64) -> None:
        """No operation, use to make dummy app calls to increase opcode budget"""
        pass

    @abimethod
    def deposit(
        self,
        proof: DynamicArray[Bytes32],
        public_inputs: DynamicArray[Bytes32],
            # amount
            # commitment
        sender: Address,
    ) -> tuple[UInt64, Bytes32]: # return commitment leaf index and tree root
        """Deposit funds.
           This transaction must be signed by the deposit verifier which verifies the
           zk-proof and public inputs, and be followed by a payment transaction with sender
           matching the `sender` argument
        """
        py.ensure_budget(deposit_opcode_budget_opup, fee_source=py.OpUpFeeSource.GroupCredit)

        # Extract the amount and commitment from the public inputs
        amount = value_from_Bytes32(public_inputs[0].copy())
        commitment = public_inputs[1].copy()

        # Verify the proof was validated by the deposit verifier logicsig
        # by checking the transaction is signed by the deposit verifier
        assert Txn.sender == TemplateVar[Account]("DEPOSIT_VERIFIER_ADDRESS"), (
            "Transaction is not signed by the deposit verifier")

        # Check next transaction in the group is a payment of `amount` to the application,
        # the amount is at least the minimum deposit, and the sender is the expected one
        pay_txn = py.gtxn.PaymentTransaction(op.Txn.group_index + 1)
        assert pay_txn.receiver == Global.current_application_address, "Wrong receiver"
        assert pay_txn.amount == amount, "Incorrect amount received"
        assert pay_txn.amount >= deposit_minimum_amount, "Amount is less than minimum deposit"
        assert pay_txn.sender == sender, "Sender is not the expected one"

        # Fail if the tree is full, no more deposit accepted
        assert self.tree_not_full(), "Tree is full"

        # Save the commitment in the tree
        self.update_tree_with(commitment)

        return (self.inserted_leaves_count - 1, self.root.copy())

    @abimethod
    def withdraw(
        self,
        proof: DynamicArray[Bytes32],
        public_inputs: DynamicArray[Bytes32],
            # recipient_mod (address mod curve_mod)
            # withdrawal
            # fee
            # commitment
            # nullifier
            # root
        recipient: Account,
        no_change: Bool,
        extra_txn_fee: UInt64
    ) -> tuple[UInt64, Bytes32]: # return commitment leaf index and tree root
        """Withdraw funds.

           This transaction must be signed by the withdrawal verifier which verifies the
           zk-proof and public inputs.

           The optional argument `no_change` is used to instruct the contract to not
           add the change to the tree; this is meant to be used when the tree is full.
           If used and the user does not withdraw the full amount available, the change
           will be lost.

           The optional argument `extra_txn_fee` is used to indicate that the user
           wants to pay that as additional transaction fee to the blockchain on top
           of the minimum fee (e.g., if there is congestion). This amount will
           be subtracted from the withdrawal amount. This can be useful when
           calling the TSS (treasury smart signature) to pay the transaction fees.
        """
        py.ensure_budget(withdrawal_opcode_budget_opup, fee_source=py.OpUpFeeSource.GroupCredit)

        # Extract the public input
        recipient_mod = public_inputs[0].copy()
        withdrawal = public_inputs[1].copy()
        fee = public_inputs[2].copy()
        commitment = public_inputs[3].copy()
        nullifier = public_inputs[4].copy()
        root = public_inputs[5].copy()

        # Check mod of recipient address matches recipient_mod
        assert recipient_mod == Bytes32.from_bytes(
            py.op.bzero(32)
            |
            (py.BigUInt.from_bytes(recipient.bytes) % curve_mod).bytes
        ), "Recipient address mod does not match"

        # Verify the proof was validated by the withdrawal verifier logicsig
        # by checking the transaction is signed by the withdrawal verifier
        assert Txn.sender == TemplateVar[py.Account]("WITHDRAWAL_VERIFIER_ADDRESS"), (
            "Transaction is not signed by the withdrawal verifier")

        # Add the nullifier to the spent nullifiers, or fail if it already exists
        assert op.Box.create(nullifier.bytes, 0), "Nullifier already exists"

        # Check the root is valid
        assert valid_root(root), "Invalid root"

        # Check the fee is at least max(0.1 algo, 0.1% of the withdrawal amount)
        withdrawal_amount = value_from_Bytes32(withdrawal)
        fee_amount = value_from_Bytes32(fee)

        min_fee = withdrawal_amount // withdrawal_fee_divisor
        if min_fee < withdrawal_minimum_fee:
            min_fee = UInt64(withdrawal_minimum_fee)

        assert fee_amount >= min_fee, "Fee too low"

        # Check the optional extra transaction fee can be covered
        assert extra_txn_fee <= withdrawal_amount, "Extra transaction fee cannot be covered"

        # Pay the recipient
        itxn.Payment(
            receiver=recipient,
            amount=withdrawal_amount - extra_txn_fee,
            fee = 0
        ).submit()

        # Pay the the protocol treasury smart signature (TSS) but keep a portion of the fee
        # to fund the nullifier box MBR
        itxn.Payment(
            receiver=self.TSS,
            amount=fee_amount + extra_txn_fee - nullifier_MBR,
            fee=0
        ).submit()

        # Save the change commitment, unless no_change is set or the tree is full
        if not no_change.native:
            assert self.tree_not_full(), "Tree is full"
            self.update_tree_with(commitment)

        return (self.inserted_leaves_count - 1, self.root.copy())

    @subroutine
    def tree_not_full(self) -> bool:
        """Check if the tree is full"""
        return self.inserted_leaves_count < UInt64(max_leaves)

    @subroutine
    def add_root(self, root: Bytes32) -> None:
        """Add a new root to self and to the list of last roots"""
        self.root = root.copy()
        op.Box.replace(b'roots', self.next_root_index*32, self.root.bytes)
        self.next_root_index = (self.next_root_index + 1) % roots_count

    @subroutine
    def update_tree_with(self, leafHash: Bytes32) -> None:
        """Update the Merkle tree with a new leaf hash."""
        # The initial value for each node at each level of the tree
        # This is based on the mimc_bn254 hash of []byte{0}
        zero_hashes = StaticArray[Bytes32, typing.Literal[32]].from_bytes(
            Bytes.from_hex(
                "2c7298fd87d3039ffea208538f6b297b60b373a63792b4cd0654fdc88fd0d6ee"
                + "299efaa989f174feff2bbeab19c570216848e2ce4104be7c3fb9fdf8aa9de707"
                + "26d972fcebd66eb80d0abcf0f8693cd26cf235afe7667ea57c4d5afd024c9253"
                + "145355664318fec418eebeaf871abae0b6fd9daaafe57c4a996c78d6b6e899fe"
                + "1e168dd00ae42c342d113730f6d03a9c817e07f9d53f5c667db6019869139b19"
                + "0721348941259d9749e6158c2e1415f686478c99c302fad4a89013c9bed9383d"
                + "1e36919cbca2c72a6985ba44cd7f903a3473309833a0db2d9ebc28911d1dc5af"
                + "1e19f9b309d37cd0e485a51f245d90897455d915565015ec21555a573554fd99"
                + "163307c51e5f49657d29c4c4182e5e15c5b00c112fc229a309ea228c7cb8f7af"
                + "1ec84059f46d162c3bb2a26cdb7645683581082f23f04a70822770d927e1d9a9"
                + "277246058e29ea281b59072bce46aa865f6645bcc2351484e91efd491e968f69"
                + "25bade792d04a8ad56b011a4a2437bdf4e29115ef91615c98ff7db63f14f9edd"
                + "12674d23ad24945abff7df5f6e26386588723c7fb868b57805fc30533a3e7e4e"
                + "28731c90e764d86ba6b303ee880c033072caa89234a00353bb710469b8393723"
                + "2bd7c9f78f6f2a6ead15292f8746597494de38a942ce3c410bbb810f1bb0b526"
                + "23043138adbec7b44830018984fcbac393b02307e674580700042931cfac9b6f"
                + "05f93648dbd103dfaca8b4a45a6122e5eccaa32aeb5a0833700de9a58f8cbf8c"
                + "2d9918579c9fc07ecb6a07faf51b71b7df9dfb2c40a7fe2a6b4e2694dc7faf2d"
                + "143f87908ad366917e86cd99282d9c48a436ec41745c94852e74c548fdecb2c9"
                + "2ec6b723a0d20eda0ba7ba3bf54d16ae5aa8f962727c6653af724fe4f4bc4325"
                + "11e77fce4a9991c23fd9f0c394c0aa8e75cf1ab9508ef2da9672bbd8ae2eccf7"
                + "29e5fd751af86d5a3d97688bcb875c1b793fe94d2b037ab459ff4026f381f2ce"
                + "03e29c0702b8344efb5c544ebfc7e5c45cd1e56dfec46abf0cabe50db26d91f3"
                + "1e458ea5fa4b33c125acfe8e65e66f1a1b19c3c7c91f6053625066e72e364d4a"
        ))

        # if we are initializing the tree, set subtree to the zero hashes
        if leafHash == Bytes32.from_bytes(b''):
            op.Box.put(b'subtree', zero_hashes.bytes)
            self.add_root(Bytes32.from_bytes(Bytes.from_hex(
                "058169ffe87b9033238369464bb6aea8ffd76c944dc3ed98ad9f3f2d91357968")))
            return

        subtree, exist = op.Box.get(b'subtree')
        index = self.inserted_leaves_count
        currentHash = leafHash.bytes
        for i in urange(tree_depth):
            if index & 1 == 0:
                subtree = op.replace(subtree, i*32, currentHash)
                left = currentHash
                right = zero_hashes[i].bytes
            else:
                left = subtree[i*32:(i+1)*32]
                right = currentHash

            currentHash = op.mimc(op.MiMCConfigurations.BN254Mp110, left + right)
            index = index >> 1

        op.Box.replace(b'subtree', 0, subtree)
        self.add_root(Bytes32.from_bytes(currentHash))
        self.inserted_leaves_count += 1


@subroutine
def valid_root(root: Bytes32) -> bool:
    """Check if the root is included in the last saved roots"""
    roots, exist = op.Box.get(b'roots')
    for i in urange(roots_count):
        r = roots[i*32:(i+1)*32]
        if r == root.bytes:
            return True
    return False

@subroutine
def value_from_Bytes32(amount: Bytes32) -> UInt64:
    """Convert an amount encoded in a Bytes32 to a UInt64"""
    return op.btoi(amount.bytes[24:32])
