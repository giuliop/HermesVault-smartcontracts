package test

import (
	"context"
	"fmt"
	"log"
	"math/big"
	"os"
	"testing"

	"github.com/giuliop/HermesVault-smartcontracts/avm"
	"github.com/giuliop/HermesVault-smartcontracts/config"
	"github.com/giuliop/HermesVault-smartcontracts/deployed"
	"github.com/giuliop/HermesVault-smartcontracts/setup"

	"github.com/algorand/go-algorand-sdk/v2/crypto"
	"github.com/algorand/go-algorand-sdk/v2/transaction"
	"github.com/algorand/go-algorand-sdk/v2/types"
)

var (
	f         *Frontend
	dummyLsig *Lsig
)

const (
	SucceedTEAL = `
#pragma version 2
int 1`
)

func TestMain(m *testing.M) {
	setup.CreateApp(deployed.DevNet)
	f = NewAppFrontend()

	dummyLsigBytes, err := avm.CompileTeal([]byte(SucceedTEAL))
	if err != nil {
		fmt.Printf("Error compiling dummy teal: %s", err)
		os.Exit(1)
	}
	dummyLsig = readLogicSig(dummyLsigBytes)

	code := m.Run() // run all tests in package
	// teardown if needed

	os.Exit(code)
}

func TestDepositWithdrawMBR(t *testing.T) {
	account := crypto.GenerateAccount()
	err := avm.EnsureFunded(account.Address.String(), 10000*1e6)
	if err != nil {
		t.Fatalf("Error funding account: %s", err)
	}

	// check initial MBR is correct
	mbr := avm.MBR(f.App.Id)
	if mbr != config.InitialMbr {
		t.Fatalf("Initial MBR different than expected %d, got %d", config.InitialMbr, mbr)
	}

	depositAmount := uint64(100 * 1e6)
	deposit, err := f.SendDeposit(&account, depositAmount)
	if err != nil {
		t.Fatalf("Error making deposit: %s", err)
	}
	fmt.Printf("Deposit made at trasactions: %v by %s\n", deposit.TxnIds[0],
		account.Address.String())

	// let's keep track of the number of withdrawals to check the final MBR
	withdrawalCount := 0
	// let's make a withdrawal to a funded account
	firstWithdrawalAmount := uint64(5 * 1e6)
	firstWithdrawalOpts := &WithdrawalOpts{
		recipient:    account.Address,
		feeRecipient: account.Address,
		feeSigner:    transaction.BasicAccountTransactionSigner{Account: account},
		amount:       firstWithdrawalAmount,
		fromNote:     deposit.Note,
	}
	firstWithdrawal, err := f.SendWithdrawal(firstWithdrawalOpts)
	if err != nil {
		t.Fatalf("Error making withdrawal: %s", err)
	}
	fmt.Printf("Withdrawal made at transactions: %v by %s with change of %v\n",
		firstWithdrawal.TxnIds[0], account.Address.String(), firstWithdrawal.Note.Amount)
	withdrawalCount++

	newAccount := crypto.GenerateAccount()

	// now let's make a withdrawal to a new account using the TSS, withdrawing everything
	fee := config.WithdrawalMinFeeMultiplier*transaction.MinTxnFee + config.NullifierMbr
	availableToWithdraw := depositAmount - firstWithdrawalAmount - uint64(2*fee)
	secondWithdrawalOpts := &WithdrawalOpts{
		recipient: newAccount.Address,
		amount:    availableToWithdraw,
		fromNote:  firstWithdrawal.Note,
	}
	secondWithdrawal, err := f.SendWithdrawal(secondWithdrawalOpts)
	if err != nil {
		t.Fatalf("Error making withdrawal: %s", err)
	}
	fmt.Printf("Withdrawal made at transactions: %v by %s with change of %v\n",
		secondWithdrawal.TxnIds[0], account.Address.String(), secondWithdrawal.Note.Amount)
	withdrawalCount++

	// Let's try one more withdrawal, it should fail because the last change is zero
	thirdWithdrawalOpts := secondWithdrawalOpts
	thirdWithdrawalOpts.amount = 1
	_, err = f.SendWithdrawal(thirdWithdrawalOpts)
	if err != nil {
		fmt.Println("Error making withdrawal, as expected")
	} else {
		t.Fatalf("Withdrawal should have failed but it didn't")
	}

	// now we make 1 deposit and `rootsCount` * 2 withdrawal to test correct root management
	deposit, err = f.SendDeposit(&account, 1000*1e6)
	if err != nil {
		t.Fatalf("Error making deposit: %s", err)
	}
	note := deposit.Note
	newAccount = crypto.GenerateAccount()
	withdrawalOpts := &WithdrawalOpts{
		recipient: newAccount.Address,
		amount:    0.1 * 1e6,
		fromNote:  note,
	}
	for i := 1; i <= config.RootsCount*2; i++ {
		w, err := f.SendWithdrawal(withdrawalOpts)
		if err != nil {
			t.Fatalf("Error making withdrawal %d/100: %s", i, err)
		}
		withdrawalOpts.fromNote = w.Note
		withdrawalCount++
	}

	// check final MBR is as expected
	mbr = avm.MBR(f.App.Id)
	if mbr != config.InitialMbr+withdrawalCount*config.NullifierMbr {
		t.Fatalf("Final MBR different than expected %d, got %d", config.InitialMbr, mbr)
	}
}

func TestWrongLsigVerifier(t *testing.T) {
	account := crypto.GenerateAccount()
	err := avm.EnsureFunded(account.Address.String(), 10000*1e6)
	if err != nil {
		t.Fatalf("Error funding account: %s", err)
	}

	depositAmount := uint64(10 * 1e6)
	depositLsig := f.App.DepositVerifier
	f.App.DepositVerifier = dummyLsig
	_, err = f.SendDeposit(&account, depositAmount)
	if err == nil {
		t.Fatalf("Ouch, no error making deposit with dummy lsig: %s", err)
	}
	fmt.Println("Error making deposit with dummy lsig as expected")

	f.App.DepositVerifier = depositLsig
	// let's make a deposit and then try to make a withdrawal with the dummy lsig

	deposit, err := f.SendDeposit(&account, depositAmount)
	if err != nil {
		t.Fatalf("Error making deposit: %s", err)
	}
	fmt.Printf("Deposit made at trasactions: %v by %s\n", deposit.TxnIds[0],
		account.Address.String())

	withdrawalLsig := f.App.WithdrawalVerifier
	f.App.WithdrawalVerifier = dummyLsig
	_, err = f.SendWithdrawal(&WithdrawalOpts{
		recipient: account.Address,
		amount:    depositAmount - 1*1e6,
		fromNote:  deposit.Note,
	})
	if err == nil {
		t.Fatalf("Ouch, no error making withdrawal with dummy lsig: %s", err)
	}
	fmt.Println("Error making withdrawal with dummy lsig as expected")
	f.App.WithdrawalVerifier = withdrawalLsig
}

func TestWithdrawToAddressBiggerThanMod(t *testing.T) {
	biggerThanModAddress := generateBiggerThanModAddress()

	// let's make a deposit
	depositorAccount := crypto.GenerateAccount()
	err := avm.EnsureFunded(depositorAccount.Address.String(), 1000*1e6)
	if err != nil {
		t.Fatalf("Error funding account: %s", err)
	}
	depositAmount := uint64(10 * 1e6)
	deposit, err := f.SendDeposit(&depositorAccount, depositAmount)
	if err != nil {
		t.Fatalf("Error making deposit: %s", err)
	}

	// let's try to make a withdrawal to the bigger than mod address
	withdrawAmount := uint64(1 * 1e6)
	withdrawalOpts := &WithdrawalOpts{
		recipient: biggerThanModAddress,
		amount:    withdrawAmount,
		fromNote:  deposit.Note,
	}
	withdrawal, err := f.SendWithdrawal(withdrawalOpts)
	if err != nil {
		t.Fatalf("Error making withdrawal: %s", err)
	}
	fmt.Printf("Withdrawal made at transactions: %v by %s with change of %v\n",
		withdrawal.TxnIds[0], depositorAccount.Address.String(), withdrawal.Note.Amount)

	// let's check the recipient address got the tokens
	algod := avm.GetAlgodClient()
	accountInfo, err := algod.AccountInformation(biggerThanModAddress.String()).
		Do(context.Background())
	if err != nil {
		log.Fatalf("Error fetching account information: %v", err)
	}

	if accountInfo.Amount != withdrawAmount {
		t.Fatalf("Expected balance %d, got %d", withdrawAmount, accountInfo.Amount)
	}

}

func generateBiggerThanModAddress() types.Address {
	mod := config.Curve.ScalarField()
	for i := 0; ; i++ {
		address := crypto.GenerateAccount().Address
		addressAsNumber := new(big.Int).SetBytes(address[:])
		if addressAsNumber.Cmp(mod) > 0 {
			fmt.Printf("Found address bigger than mod: %s at iteration %d ",
				address.String(), i+1)
			return address
		}
	}
}
