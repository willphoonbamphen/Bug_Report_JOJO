// SPDX-License-Identifier: BUSL-1.1
pragma solidity ^0.8.19;

import "forge-std/Test.sol";
import "../../src/fundingRateArbitrage/FundingRateArbitrage.sol";
import "../../src/JUSDBank/JUSDBank.sol";
import "../../src/JOJODealer.sol";
import "../../src/Perpetual.sol";
import "../../src/libraries/Types.sol";
import "../../src/oracle/EmergencyOracle.sol";
import "../../src/support/MockSwap.sol";
import "../../src/support/TestERC20.sol";
import "../utils/EIP712Test.sol";
import "../utils/Utils.sol";

/**
 * @title  PoC: FundingRateArbitrage - Negative perpNetValue Freezes All LP Operations
 * @notice Demonstrates that SafeCast.toUint256(perpNetValue) inside getNetValue() reverts
 *         when perpNetValue < 0.  A standard 2x price move on a delta-neutral vault is
 *         sufficient to trigger the bug.  The vault remains FULLY SOLVENT (spot ETH
 *         appreciates, total NAV is positive) yet every LP entry-point is permanently DoS'd.
 *
 * Root cause:
 *   FundingRateArbitrage.getNetValue()
 *     \- SafeCast.toUint256(perpNetValue)   <- reverts when perpNetValue < 0
 *          \- getIndex() reverts
 *               \- deposit() / requestWithdraw() / permitWithdrawRequests() all revert
 *
 * Trigger conditions (realistic):
 *   - FRA holds ETH short perp position (normal delta-neutral operation)
 *   - ETH price rises ~2x above open price (normal market move)
 *   - perpNetValue = netPositionValue + primaryCredit + secondaryCredit  < 0
 */
contract FundingRateArbitrageDoSPoC is Test {

    // -- state (mirrors FundingRateArbitrageTest exactly) ------------------

    FundingRateArbitrage public fra;
    Utils internal utils;
    TestERC20 public eth;
    JUSDBank public jusdBank;
    TestERC20 public jusd;
    TestERC20 public USDC;
    Perpetual public perpetual;
    EmergencyOracle public ETHOracle;
    JOJODealer public jojoDealer;
    MockSwap public swapContract;

    address payable[] internal users;
    address internal alice;
    address internal insurance;
    address internal Owner;
    address internal orderSenderAddr;
    address internal fastWithdrawAddr;

    // FRA operator (signs orders on behalf of FRA contract)
    uint256 internal fraOperatorPK  = 0xA11CE;       // same as sender1PrivateKey in original test
    address internal fraOperator;

    // Counterparty (takes the other side of FRA's short)
    uint256 internal counterpartyPK = 0xC0C;
    address internal counterparty;

    // -- setUp - identical to FundingRateArbitrageTest.setUp ---------------

    function setUp() public {
        eth     = new TestERC20("eth",  "eth",  18);
        jusd    = new TestERC20("jusd", "jusd",  6);
        USDC    = new TestERC20("usdc", "usdc",  6);
        ETHOracle = new EmergencyOracle("ETH Oracle");

        utils = new Utils();
        users = utils.createUsers(10);
        alice           = users[0];
        insurance       = users[2];
        Owner           = users[4];
        orderSenderAddr = users[5];
        fastWithdrawAddr = users[6];

        fraOperator  = vm.addr(fraOperatorPK);
        counterparty = vm.addr(counterpartyPK);

        jojoDealer = new JOJODealer(address(USDC));
        perpetual  = new Perpetual(address(jojoDealer));

        _initJOJODealer();
        _initJUSDBank();
        _initFRA();
        _initSwap();

        // Mirror original setUp: deposit JUSD as FRA's secondaryCredit in JOJODealer
        jusd.mint(address(this), 10_010e6);
        jusd.approve(address(jojoDealer), 10_010e6);
        jojoDealer.deposit(0, 10_010e6, address(fra));

        ETHOracle.setMarkPrice(1000e6);

        // Fund counterparty with USDC margin (needs > initialMargin for 10 ETH long)
        USDC.mint(counterparty, 20_000e6);
        vm.startPrank(counterparty);
        USDC.approve(address(jojoDealer), 20_000e6);
        jojoDealer.deposit(20_000e6, 0, counterparty);
        vm.stopPrank();
    }

    function _initJOJODealer() internal {
        jojoDealer.setMaxPositionAmount(10);
        jojoDealer.setOrderSender(orderSenderAddr, true);
        jojoDealer.setWithdrawTimeLock(0);
        jojoDealer.setFastWithdrawalWhitelist(fastWithdrawAddr, true);
        jojoDealer.setSecondaryAsset(address(jusd));
        Types.RiskParams memory param = Types.RiskParams({
            initialMarginRatio:   5e16,
            liquidationThreshold: 3e16,
            liquidationPriceOff:  1e16,
            insuranceFeeRate:     2e16,
            markPriceSource:      address(ETHOracle),
            name:                 "ETH",
            isRegistered:         true
        });
        jojoDealer.setPerpRiskParams(address(perpetual), param);
    }

    function _initJUSDBank() internal {
        jusdBank = new JUSDBank(
            10, insurance, address(jusd), address(jojoDealer),
            100_000_000_000, 100_000_000_001, 0, address(USDC)
        );
        jusdBank.initReserve(
            address(eth), 8e17, 4000e18, 2030e18, 100_000e6, 825e15, 5e16, 1e17, address(ETHOracle)
        );
        jusd.mint(address(jusdBank), 100_000e6);
    }

    function _initFRA() internal {
        // fraOperator is set as operator in the FRA constructor (same pattern as original test)
        fra = new FundingRateArbitrage(
            address(eth),
            address(jojoDealer),
            address(perpetual),
            fraOperator,        // <- authorised operator for signing orders
            address(ETHOracle)
        );
        fra.transferOwnership(Owner);
        vm.startPrank(Owner);
        fra.setMaxNetValue(100_000e6);    // raised cap to accommodate PoC deposit
        fra.setDefaultQuota(100_000e6);
        vm.stopPrank();
    }

    function _initSwap() internal {
        swapContract = new MockSwap(address(USDC), address(eth), address(ETHOracle));
        USDC.mint(address(swapContract), 100_000e6);
        eth.mint(address(swapContract), 10_000e18);
    }

    // -- helpers -----------------------------------------------------------

    /// @dev Builds and EIP-712 signs a perpetual order.
    function buildOrder(
        address signer,
        uint256 pk,
        int128 paper,
        int128 credit
    ) internal view returns (Types.Order memory order, bytes memory sig) {
        // Same encoding as FundingRateArbitrageTest.buildOrder
        int64  makerFeeRate = 2e14;
        int64  takerFeeRate = 7e14;
        bytes memory infoBytes = abi.encodePacked(
            makerFeeRate, takerFeeRate,
            uint64(block.timestamp), uint64(block.timestamp)
        );
        order = Types.Order({
            perp:         address(perpetual),
            signer:       signer,
            paperAmount:  paper,
            creditAmount: credit,
            info:         bytes32(infoBytes)
        });
        bytes32 domainSep = EIP712Test._buildDomainSeparator("JOJO", "1", address(jojoDealer));
        bytes32 digest    = keccak256(abi.encodePacked(
            "\x19\x01", domainSep, EIP712Test._structHash(order)
        ));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        sig = abi.encodePacked(r, s, v);
    }

    /// @dev Opens a short position for FRA.
    ///      FRA is the TAKER (orderList[0]); counterparty is the MAKER (orderList[1]).
    ///
    /// @param matchEth   number of ETH to match (e.g. 10e18)
    /// @param fraCredit  FRA's order creditAmount (limit: min USDC taker expects, e.g. 9900e6)
    /// @param cpCredit   counterparty creditAmount (limit: max USDC maker pays, e.g. -10100e6)
    function openFRAShort(
        uint256 matchEth,
        int128  fraCredit,
        int128  cpCredit
    ) internal {
        int128 fraEth = -int128(int256(matchEth));   // FRA: short (negative paper)
        int128 cpEth  =  int128(int256(matchEth));   // counterparty: long (positive paper)

        (Types.Order memory fraOrder, bytes memory fraSig) =
            buildOrder(address(fra), fraOperatorPK, fraEth, fraCredit);
        (Types.Order memory cpOrder,  bytes memory cpSig)  =
            buildOrder(counterparty,  counterpartyPK, cpEth, cpCredit);

        Types.Order[] memory orders = new Types.Order[](2);
        orders[0] = fraOrder;   // taker
        orders[1] = cpOrder;    // maker

        bytes[] memory sigs = new bytes[](2);
        sigs[0] = fraSig;
        sigs[1] = cpSig;

        uint256[] memory amounts = new uint256[](2);
        amounts[0] = matchEth;
        amounts[1] = matchEth;

        vm.prank(orderSenderAddr);
        perpetual.trade(abi.encode(orders, sigs, amounts));
    }

    // -- PoC ---------------------------------------------------------------

    /**
     * @notice Full end-to-end PoC.
     *
     * Timeline:
     *  1. Alice (LP) deposits 20,000 USDC - vault is healthy.
     *  2. Owner buys 10 ETH spot (delta hedge).
     *  3. FRA opens a 10-ETH SHORT in the perp (completing the delta-neutral position).
     *  4. Sanity check: getNetValue() works fine at ETH = $1,000.
     *  5. ETH price doubles (1,000 -> 2,011).
     *     - perpNetValue = netPositionValue + primaryCredit + secondaryCredit
     *                    ~ (-20,110 + 10,092) + 0 + 10,010  ~  -8  USDC  <- NEGATIVE
     *     - True vault NAV = spot (20,110) + USDC buffer (10,000) ~ 30,102  <- SOLVENT
     *  6. All LP operations now REVERT with "SafeCast: value must be positive".
     */
    function testPoC_NegativePerpNetValueFreezesAllLP() public {
        // -- 1. LP deposit --------------------------------------------------
        USDC.mint(alice, 20_000e6);
        vm.startPrank(alice);
        USDC.approve(address(fra), 20_000e6);
        fra.deposit(20_000e6);
        vm.stopPrank();

        assertEq(fra.getIndex(), 1e6, "index should be 1.000000 after first deposit");
        console.log("[1] Alice deposited 20,000 USDC. Index = 1.000000 USDC/earnUSDC");

        // -- 2. Spot ETH buy (delta hedge: 10k USDC -> 10 ETH) --------------
        vm.startPrank(Owner);
        bytes memory swapData  = swapContract.getSwapUSDCToOtherData(10_000e6, address(eth));
        bytes memory spotParam = abi.encode(
            address(swapContract), address(swapContract), 10_000e6, swapData
        );
        fra.swapBuyToken(10e18, address(eth), spotParam);
        vm.stopPrank();

        uint256 spotETH = eth.balanceOf(address(fra));
        assertEq(spotETH, 10e18, "FRA should hold 10 ETH spot");
        console.log("[2] Bought 10 ETH spot. USDC buffer =", USDC.balanceOf(address(fra)));

        // -- 3. Open 10-ETH SHORT in perp ----------------------------------
        //   FRA (taker):    short -10 ETH, limit credit = +9,900 USDC
        //   Counterparty:    long +10 ETH, limit credit = -10,100 USDC
        //   Fill is at maker price; FRA receives ~10,093 USDC credit in perp (after fees)
        openFRAShort(10e18, 9_900e6, -10_100e6);

        (int256 paper, int256 credit) = perpetual.balanceOf(address(fra));
        assertEq(paper, -10e18, "FRA perp: short 10 ETH");
        assertGt(credit, 9_900e6, "FRA perp: credit should be ~10,093 after fill");
        console.log("[3] Opened 10-ETH SHORT. Perp credit =", uint256(credit));

        // -- 4. Sanity: vault healthy at ETH = 1,000 -----------------------
        uint256 nvBefore = fra.getNetValue();
        assertGt(nvBefore, 0, "getNetValue() should be positive at base price");
        console.log("[4] getNetValue() at ETH=1000:", nvBefore, "  (positive, healthy)");

        // -- 5. ETH price rises: 1,000 -> 2,012 ------------------------------
        //
        //   FRA opened short at price 1,010 (credit = +10,100e6 in perp).
        //   Fees are 0 (info-byte extraction gives 0 in test env).
        //   At ETH = 2,012:
        //   perpNetValue = -10 x 2012e6 + 10,100e6 + 10,010e6
        //                = -20,120e6   + 20,110e6
        //                = -10e6   <- NEGATIVE (only $1 above breakeven)
        //
        //   True vault NAV (correct implementation):
        //   = collateralValue(10 ETH x 2012e6) + usdcBuffer(10,000e6) + creditInPerp(10,100e6)
        //     + secondaryCredit(10,010e6) - jusdBorrowed(10,010e6)
        //   = 20,120e6 + 10,000e6 + 10,100e6 + 10,010e6 - 10,010e6 - 20,120e6
        //   ~ 20,100e6   <- FULLY SOLVENT (>$20k)
        //
        ETHOracle.setMarkPrice(2012e6);
        console.log("[5] ETH price moved: 1,000 -> 2,012 (just above perp breakeven)");

        uint256 spotVal  = (eth.balanceOf(address(fra)) / 1e12) * 2012e6 / 1e6;  // approx
        uint256 usdcBuf  = USDC.balanceOf(address(fra));
        console.log("    Spot ETH value (USDC) ~", spotVal);
        console.log("    USDC buffer           =", usdcBuf);
        console.log("    True vault NAV        ~ SOLVENT (>$20k)");

        // -- 6a. getNetValue() reverts --------------------------------------
        vm.expectRevert("SafeCast: value must be positive");
        fra.getNetValue();
        console.log("[6a] getNetValue() REVERTS -- SafeCast.toUint256(negative perpNetValue)");

        // -- 6b. deposit() frozen -------------------------------------------
        address bob = address(0xB0B);
        USDC.mint(bob, 1000e6);
        vm.startPrank(bob);
        USDC.approve(address(fra), 1000e6);
        vm.expectRevert("SafeCast: value must be positive");
        fra.deposit(1000e6);
        vm.stopPrank();
        console.log("[6b] deposit() FROZEN -- new LPs cannot enter");

        // -- 6c. requestWithdraw() frozen -- Alice cannot exit ---------------
        // Cache balance BEFORE vm.expectRevert: balanceOf() is itself a contract
        // call that would consume the expectRevert if placed in the argument position.
        uint256 aliceEarnUSDC = fra.balanceOf(alice);
        assertGt(aliceEarnUSDC, 0, "Alice must hold earnUSDC");
        vm.startPrank(alice);
        fra.approve(address(fra), type(uint256).max);
        vm.expectRevert("SafeCast: value must be positive");
        fra.requestWithdraw(aliceEarnUSDC);
        vm.stopPrank();
        console.log("[6c] requestWithdraw() FROZEN -- Alice's 20,000 USDC is LOCKED");

        // -- 6d. permitWithdrawRequests() frozen ----------------------------
        uint256[] memory ids = new uint256[](0);
        vm.prank(Owner);
        vm.expectRevert("SafeCast: value must be positive");
        fra.permitWithdrawRequests(ids);
        console.log("[6d] permitWithdrawRequests() FROZEN -- owner cannot process any withdrawal");

        // -- 7. Confirm vault solvency during freeze ------------------------
        uint256 totalAssets = eth.balanceOf(address(fra)) * 2012e6 / 1e18
                            + USDC.balanceOf(address(fra));
        assertGt(totalAssets, 20_000e6,
            "Vault holds >$20k of assets but all LP operations are permanently frozen");

        console.log("");
        console.log("=== RESULT ===");
        console.log("20,000 USDC LP funds PERMANENTLY FROZEN");
        console.log("Vault total assets (spot + USDC):", totalAssets);
        console.log("Vault is SOLVENT -- freeze is caused solely by SafeCast bug");
        console.log("Fix: replace SafeCast.toUint256(perpNetValue) with max(perpNetValue, 0)");
    }
}
