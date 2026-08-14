package clob

import (
	"math/big"
	"strings"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/signer/core/apitypes"
)

func mustBig(s string) *big.Int {
	n, _ := new(big.Int).SetString(s, 10)
	return n
}

const testKey = "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"

func TestWalletAddress(t *testing.T) {
	w, err := NewWallet("0x" + testKey)
	if err != nil {
		t.Fatal(err)
	}
	if w.Address == (common.Address{}) {
		t.Fatal("adres bos")
	}
}

func TestOrderAmountsBox040(t *testing.T) {
	// 5 hisse @ 0.40 BUY -> maker 2.00 USDC (2000000), taker 5 share (5000000)
	m, tk := orderAmounts(Buy, 5, 0.40)
	if m.String() != "2000000" || tk.String() != "5000000" {
		t.Fatalf("BUY amounts yanlis: maker=%s taker=%s", m, tk)
	}
	// SELL tersine
	m2, tk2 := orderAmounts(Sell, 5, 0.40)
	if m2.String() != "5000000" || tk2.String() != "2000000" {
		t.Fatalf("SELL amounts yanlis: maker=%s taker=%s", m2, tk2)
	}
}

func TestBuildAndSignRecoversSigner(t *testing.T) {
	w, err := NewWallet("0x" + testKey)
	if err != nil {
		t.Fatal(err)
	}
	exchange := "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
	so, err := w.buildAndSign(exchange, 137, "123456789", Buy, 5, 0.40, "", 0)
	if err != nil {
		t.Fatal(err)
	}
	if so.MakerAmount != "2000000" || so.TakerAmount != "5000000" || so.Side != "BUY" {
		t.Fatalf("imzali emir alanlari yanlis: %+v", so)
	}
	sigHex := strings.TrimPrefix(so.Signature, "0x")
	if len(sigHex) != 130 { // 65 bayt
		t.Fatalf("imza uzunlugu 65 bayt olmali, %d hex", len(sigHex))
	}
	// Digest'i yeniden kurup imzadan imzalayiciyi geri cikar -> adres eslesmeli
	o := order{
		Salt: mustBig(so.Salt), Maker: w.Address, Signer: w.Address,
		Taker: common.HexToAddress(zeroAddress), TokenID: mustBig(so.TokenID),
		MakerAmount: mustBig(so.MakerAmount), TakerAmount: mustBig(so.TakerAmount),
		Expiration: mustBig("0"), Nonce: mustBig("0"), FeeRateBps: mustBig("0"),
		Side: Buy, SignatureType: 0,
	}
	td := w.typedData(exchange, 137, o)
	digest, _, err := apitypes.TypedDataAndHash(td)
	if err != nil {
		t.Fatal(err)
	}
	sig := common.FromHex(so.Signature)
	if sig[64] >= 27 {
		sig[64] -= 27
	}
	pub, err := crypto.SigToPub(digest, sig)
	if err != nil {
		t.Fatal(err)
	}
	if crypto.PubkeyToAddress(*pub) != w.Address {
		t.Fatal("imzadan cikan adres cuzdanla eslesmiyor")
	}
}
