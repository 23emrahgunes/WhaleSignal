// Package clob, Polymarket CLOB'a GERCEK limit/marketable emir imzalar ve gonderir.
// Referans (birebir davranis hedefi): basit-arbitraj/src/polymarket.ts +
// @polymarket/clob-client createOrder/postOrder. EOA (signatureType=0) imzalar.
//
// UYARI: makerAmount/takerAmount yuvarlamasi 0.40 gibi tam tick fiyatlarda kesin;
// tick-disi fiyatlarda clob-client ile PARITE dogrulanmadan CANLI kullanilmamali.
package clob

import (
	"crypto/ecdsa"
	"crypto/rand"
	"fmt"
	"math"
	"math/big"
	"strings"

	"github.com/ethereum/go-ethereum/common"
	gethmath "github.com/ethereum/go-ethereum/common/math"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/signer/core/apitypes"
)

type Side int

const (
	Buy  Side = 0
	Sell Side = 1
)

const (
	usdcDecimals  = 1e6 // collateral (USDC) 6 ondalik
	shareDecimals = 1e6 // outcome token 6 ondalik
	zeroAddress   = "0x0000000000000000000000000000000000000000"
)

// Wallet: PRIVATE_KEY'den turetilmis EOA imzalayici.
type Wallet struct {
	key     *ecdsa.PrivateKey
	Address common.Address
}

// NewWallet: "0x" + 64 hex ozel anahtardan cuzdan kurar (basit-arbitraj config.ts:78 dogrulamasi).
func NewWallet(privateKeyHex string) (*Wallet, error) {
	h := strings.TrimSpace(privateKeyHex)
	h = strings.TrimPrefix(h, "0x")
	if len(h) != 64 {
		return nil, fmt.Errorf("PRIVATE_KEY 0x + 64 hex olmali")
	}
	key, err := crypto.HexToECDSA(h)
	if err != nil {
		return nil, fmt.Errorf("gecersiz PRIVATE_KEY: %w", err)
	}
	return &Wallet{key: key, Address: crypto.PubkeyToAddress(key.PublicKey)}, nil
}

// order: EIP-712 imzalanacak CLOB emri (alan sirasi type tanimiyla ayni olmali).
type order struct {
	Salt          *big.Int
	Maker         common.Address
	Signer        common.Address
	Taker         common.Address
	TokenID       *big.Int
	MakerAmount   *big.Int
	TakerAmount   *big.Int
	Expiration    *big.Int
	Nonce         *big.Int
	FeeRateBps    *big.Int
	Side          Side
	SignatureType int
}

// orderAmounts: BUY -> maker USDC verir (size*price), taker share alir (size).
// SELL -> maker share verir, taker USDC alir. 0.40 gibi tam fiyatta kesin.
func orderAmounts(side Side, size, price float64) (maker, taker *big.Int) {
	usdc := big.NewInt(int64(math.Round(size * price * usdcDecimals)))
	shr := big.NewInt(int64(math.Round(size * shareDecimals)))
	if side == Buy {
		return usdc, shr
	}
	return shr, usdc
}

func randSalt() *big.Int {
	b := make([]byte, 32)
	_, _ = rand.Read(b)
	return new(big.Int).SetBytes(b)
}

// typedData: Polymarket CTF Exchange EIP-712 tipli veri.
func (w *Wallet) typedData(exchangeAddr string, chainID int, o order) apitypes.TypedData {
	return apitypes.TypedData{
		Types: apitypes.Types{
			"EIP712Domain": {
				{Name: "name", Type: "string"},
				{Name: "version", Type: "string"},
				{Name: "chainId", Type: "uint256"},
				{Name: "verifyingContract", Type: "address"},
			},
			"Order": {
				{Name: "salt", Type: "uint256"},
				{Name: "maker", Type: "address"},
				{Name: "signer", Type: "address"},
				{Name: "taker", Type: "address"},
				{Name: "tokenId", Type: "uint256"},
				{Name: "makerAmount", Type: "uint256"},
				{Name: "takerAmount", Type: "uint256"},
				{Name: "expiration", Type: "uint256"},
				{Name: "nonce", Type: "uint256"},
				{Name: "feeRateBps", Type: "uint256"},
				{Name: "side", Type: "uint8"},
				{Name: "signatureType", Type: "uint8"},
			},
		},
		PrimaryType: "Order",
		Domain: apitypes.TypedDataDomain{
			Name:              "Polymarket CTF Exchange",
			Version:           "1",
			ChainId:           gethmath.NewHexOrDecimal256(int64(chainID)),
			VerifyingContract: common.HexToAddress(exchangeAddr).Hex(),
		},
		Message: apitypes.TypedDataMessage{
			"salt":          o.Salt.String(),
			"maker":         o.Maker.Hex(),
			"signer":        o.Signer.Hex(),
			"taker":         o.Taker.Hex(),
			"tokenId":       o.TokenID.String(),
			"makerAmount":   o.MakerAmount.String(),
			"takerAmount":   o.TakerAmount.String(),
			"expiration":    o.Expiration.String(),
			"nonce":         o.Nonce.String(),
			"feeRateBps":    o.FeeRateBps.String(),
			"side":          fmt.Sprintf("%d", o.Side),
			"signatureType": fmt.Sprintf("%d", o.SignatureType),
		},
	}
}

// signedOrder: POST /order govdesine giden imzali emir.
type signedOrder struct {
	Salt          string `json:"salt"`
	Maker         string `json:"maker"`
	Signer        string `json:"signer"`
	Taker         string `json:"taker"`
	TokenID       string `json:"tokenId"`
	MakerAmount   string `json:"makerAmount"`
	TakerAmount   string `json:"takerAmount"`
	Expiration    string `json:"expiration"`
	Nonce         string `json:"nonce"`
	FeeRateBps    string `json:"feeRateBps"`
	Side          string `json:"side"` // "BUY" | "SELL"
	SignatureType int    `json:"signatureType"`
	Signature     string `json:"signature"`
}

// buildAndSign: bir bacak icin imzali emir uretir (tokenID string, side, size, price).
func (w *Wallet) buildAndSign(exchangeAddr string, chainID int, tokenID string, side Side, size, price float64) (*signedOrder, error) {
	tid, ok := new(big.Int).SetString(strings.TrimSpace(tokenID), 10)
	if !ok {
		return nil, fmt.Errorf("gecersiz tokenID: %q", tokenID)
	}
	maker, taker := orderAmounts(side, size, price)
	o := order{
		Salt: randSalt(), Maker: w.Address, Signer: w.Address,
		Taker: common.HexToAddress(zeroAddress), TokenID: tid,
		MakerAmount: maker, TakerAmount: taker,
		Expiration: big.NewInt(0), Nonce: big.NewInt(0), FeeRateBps: big.NewInt(0),
		Side: side, SignatureType: 0,
	}
	td := w.typedData(exchangeAddr, chainID, o)
	digest, _, err := apitypes.TypedDataAndHash(td)
	if err != nil {
		return nil, fmt.Errorf("eip712 hash: %w", err)
	}
	sig, err := crypto.Sign(digest, w.key)
	if err != nil {
		return nil, fmt.Errorf("imza: %w", err)
	}
	// go-ethereum V=0/1 -> Ethereum standardi 27/28
	if sig[64] < 27 {
		sig[64] += 27
	}
	sideStr := "BUY"
	if side == Sell {
		sideStr = "SELL"
	}
	return &signedOrder{
		Salt: o.Salt.String(), Maker: o.Maker.Hex(), Signer: o.Signer.Hex(), Taker: o.Taker.Hex(),
		TokenID: o.TokenID.String(), MakerAmount: o.MakerAmount.String(), TakerAmount: o.TakerAmount.String(),
		Expiration: "0", Nonce: "0", FeeRateBps: "0", Side: sideStr, SignatureType: 0,
		Signature: "0x" + common.Bytes2Hex(sig),
	}, nil
}
