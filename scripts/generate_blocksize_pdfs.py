from fpdf import FPDF

class BlocksizePDF(FPDF):
    def header(self):
        # self.image("docs/assets/logo.png", 10, 8, 33) 
        self.set_font("helvetica", "B", 15)
        self.cell(80)
        self.cell(30, 10, "Blocksize Capital", 0, 0, "C")
        self.ln(20)

    def footer(self):
        self.set_y(-15)
        self.set_font("helvetica", "I", 8)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}} | blocksize.info", 0, 0, "C")

    def chapter_title(self, label):
        self.set_font("helvetica", "B", 12)
        self.set_fill_color(240, 240, 240)
        self.cell(0, 8, label, 0, 1, "L", True)
        self.ln(4)

    def section_title(self, label):
        self.set_font("helvetica", "B", 10)
        self.set_text_color(79, 75, 255)
        self.cell(0, 10, label.upper(), 0, 1, "L")
        self.set_text_color(0, 0, 0)

    def body_text(self, text):
        self.set_font("helvetica", "", 10)
        self.multi_cell(0, 5, text)
        self.ln()

    def pricing_table(self, rows):
        self.set_font("helvetica", "B", 9)
        self.set_fill_color(220, 220, 220)
        self.cell(60, 8, "Tier", 1, 0, 'C', True)
        self.cell(50, 8, "Credits", 1, 0, 'C', True)
        self.cell(70, 8, "Price (USDC)", 1, 1, 'C', True)
        
        self.set_font("helvetica", "", 9)
        for row in rows:
            self.cell(60, 8, row[0], 1, 0, 'C')
            self.cell(50, 8, row[1], 1, 0, 'C')
            self.cell(70, 8, row[2], 1, 1, 'C')
        self.ln(5)

# --- POPULATION FUNCTIONS ---

def generate_docs():
    pdf = BlocksizePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.chapter_title("Institutional API Documentation")
    
    pdf.section_title("1. System Architecture")
    pdf.body_text("The Blocksize Gateway is a multi-layered infrastructure designed for automated machine-to-machine data settlement.")
    pdf.body_text("The Blocksize Gateway is a multi-layered infrastructure designed for automated machine-to-machine data settlement. (Internal Network Diagram omitted for security).")

    pdf.add_page()
    pdf.section_title("2. Operational Process")
    pdf.body_text("Each request follows a strict verification and settlement flow via the Iron Dome security layer.")
    pdf.body_text("Each request follows a strict verification and settlement flow via the Iron Dome security layer. Refer to the internal documentation for the full operational sequence.")

    pdf.section_title("3. Authentication Mode")
    pdf.body_text("- x402 Header: Include Payment-Proof signature (TX Hash).\n- Credit Header: Include X-AGENT-WALLET for drawdown.")

    pdf.output("docs/pdf/Blocksize_API_Documentation.pdf")
    print("Generated: Blocksize_API_Documentation.pdf")

def generate_catalog():
    pdf = BlocksizePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.chapter_title("Institutional Data Catalog")
    
    catalog = [
        ("Crypto Market Data", "Core", "2.0 Credits", "BTC/USD, ETH/USD, SOL/USD, 4000+ pairs"),
        ("Global Equities", "Enterprise", "8.0 Credits", "AAPL, TSLA, NVDA (US), 00700 (HK)"),
        ("FX Spot Rates", "Standard", "4.0 Credits", "EUR/USD, GBP/USD, JPY/USD (120+ pairs)"),
        ("Precious Metals", "Standard", "5.0 Credits", "XAU (Gold), XAG (Silver) vs USD/EUR"),
        ("Global Commodities", "Extended", "6.0 Credits", "BRENT, WTI, NATGAS, COPPER")
    ]
    
    for cat, tier, cost, symbols in catalog:
        pdf.section_title(cat)
        pdf.body_text(f"Service Tier: {tier}\nCost per Call: {cost}\nSymbols: {symbols}")
    
    pdf.output("docs/pdf/Blocksize_Data_Catalog.pdf")
    print("Generated: Blocksize_Data_Catalog.pdf")

def generate_flow():
    pdf = BlocksizePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.chapter_title("Autonomous User Flow")
    
    pdf.section_title("1. The 402 Challenge Loop")
    pdf.body_text("1. Agent performs GET to /v1/vwap/BTC-USD.\n2. Server returns 402 Payment Required.\n3. Server provides 'Payment-Required' header with cost and destination wallet.\n4. Agent settles USDC via Solana/Base.\n5. Agent resubmits with 'Payment-Signature' containing TX Hash.")
    
    pdf.section_title("2. Deterministic Unlock")
    pdf.body_text("Upon verification, the gateway unlocks the institutional payload for 24 hours for that specific wallet/symbol pair (unless using Credits).")
    
    pdf.output("docs/pdf/Blocksize_User_Flow.pdf")
    print("Generated: Blocksize_User_Flow.pdf")

def generate_pricing():
    pdf = BlocksizePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.chapter_title("Institutional Pricing Guide")
    
    pdf.section_title("1. Unit Economics")
    pdf.body_text("1 Credit = $0.001 Market Value. Credits allow for zero-latency drawdown without waiting for block confirmations.")

    pdf.section_title("2. Bulk Purchase Tiers")
    tiers = [
        ("Starter Pouch", "1,000 Credits", "0.90 USDC (10% OFF)"),
        ("Growth Pack", "10,000 Credits", "8.00 USDC (20% OFF)"),
        ("Institutional Vault", "100,000 Credits", "60.00 USDC (40% OFF)")
    ]
    pdf.pricing_table(tiers)
    
    pdf.section_title("3. Performance Discounts")
    pdf.body_text("Institutional clients consuming >1M calls/month receive custom rebate metadata in the X-AGENT-QUOTA response header.")
    
    pdf.output("docs/pdf/Blocksize_Pricing_Guide.pdf")
    print("Generated: Blocksize_Pricing_Guide.pdf")

def generate_agent_manual():
    pdf = BlocksizePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.chapter_title("Agent Integration Guide")
    
    pdf.section_title("1. Iron Dome Requirements")
    pdf.body_text("To prevent Sybil attacks and qualify for Trial Credits:\n- Minimum Stake: 0.1 SOL in Agent Wallet.\n- Wallet History: >24h Age and >5 Transactions.\n- IP Policy: Permanent 1-Trial-Per-IP lock.")
    
    pdf.section_title("2. Automated Discovery")
    pdf.body_text("Fetch the MCP Discovery Manifest at /mcp/manifest.json to receive full tool definitions and JSON-schema parameters.")
    
    pdf.section_title("3. Python Example")
    pdf.body_text("headers = {'X-AGENT-WALLET': wallet_address}\nresponse = requests.get(url, headers=headers)")
    
    pdf.output("docs/pdf/Blocksize_Agent_Manual.pdf")
    print("Generated: Blocksize_Agent_Manual.pdf")

def generate_state_coverage():
    pdf = BlocksizePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.chapter_title("Institutional State Data Coverage")
    
    pdf.body_text("The following assets have been qualified for institutional state price production. 'Done' status indicates full production availability across all settlement nodes.")

    # Ticker Data (Summarized for high-density reporting)
    coverage_data = [
        ("EVM", "EURA", "EUR", "Done"), ("EVM", "EURA", "USD", "Done"), ("EVM", "ALETH", "USD", "Done"),
        ("EVM", "BETH", "USD", "Done"), ("EVM", "BUSD", "BNB", "Done"), ("EVM", "BUSD", "ETH", "Done"),
        ("EVM", "BUSD", "USD", "Done"), ("EVM", "cbBTC", "USD", "Done"), ("EVM", "CBETH", "ETH", "Done"),
        ("EVM", "CBETH", "USD", "Done"), ("EVM", "CRVUSD", "USD", "Done"), ("EVM", "CUSD", "USD", "Done"),
        ("EVM", "CUSDO", "USD", "Done"), ("EVM", "deUSD", "USD", "Done"), ("EVM", "ETHx", "ETH", "Done"),
        ("EVM", "EURC", "USD", "Done"), ("EVM", "FRAX", "ETH", "Done"), ("EVM", "FRAX", "USD", "Done"),
        ("EVM", "GHO", "USD", "Done"), ("EVM", "JLP", "USD", "Done"), ("EVM", "LBTC", "BTC", "Done"),
        ("EVM", "LBTC", "USD", "Done"), ("EVM", "LISUSD", "USD", "Done"), ("EVM", "LSETH", "ETH", "Done"),
        ("EVM", "LUSD", "USD", "Done"), ("EVM", "MAG7.SSI", "USD", "Done"), ("EVM", "mETH", "ETH", "Done"),
        ("EVM", "MIM", "USD", "Done"), ("EVM", "MIMATIC", "USD", "Done"), ("EVM", "mooBIFI", "USD", "Done"),
        ("EVM", "mSOL", "USD", "Done"), ("EVM", "OETH", "ETH", "Done"), ("EVM", "OHMv2", "ETH", "Done"),
        ("EVM", "OHMv2", "USD", "Done"), ("EVM", "OUSDT", "USD", "Done"), ("EVM", "pufETH", "ETH", "Done"),
        ("EVM", "RAI", "ETH", "Done"), ("EVM", "RETH", "ETH", "Done"), ("EVM", "RSETH", "ETH", "Done"),
        ("EVM", "rswETH", "ETH", "Done"), ("EVM", "scBTC", "BTC", "Done"), ("EVM", "SCETH", "USD", "Done"),
        ("EVM", "solvBTC", "BTC", "Done"), ("EVM", "stS", "USD", "Done"), ("EVM", "SUPEROETHB", "ETH", "Done"),
        ("EVM", "SUSD", "ETH", "Done"), ("EVM", "SUSD", "USD", "Done"), ("EVM", "SWPX", "USD", "Done"),
        ("EVM", "TBTC", "BTC", "Done"), ("EVM", "TBTC", "USD", "Done"), ("EVM", "USD+", "USD", "Done"),
        ("EVM", "USD0", "USD", "Done"), ("EVM", "USD0++", "USD", "Done"), ("EVM", "USDa", "USD", "Done"),
        ("EVM", "USDf", "USD", "Done"), ("EVM", "USDL", "USD", "Done"), ("EVM", "USDM", "USD", "Done"),
        ("EVM", "USDS", "USD", "Done"), ("EVM", "USDX", "USD", "Done"), ("EVM", "USDz", "USD", "Done"),
        ("EVM", "USR", "USD", "Done"), ("EVM", "VAI", "USD", "Done"), ("EVM", "weETH", "ETH", "Done"),
        ("EVM", "WSTETH", "ETH", "Done"), ("EVM", "WSTETH", "USD", "Done"), ("Non-EVM", "JUPSOL", "USD", "Done"),
        ("Non-EVM", "KYSOL", "USD", "Done"), ("Non-EVM", "JITOSOL", "USD", "Done"), ("Non-EVM", "HSOL", "USD", "Done"),
        ("Non-EVM", "VSOL", "USD", "Done"), ("Non-EVM", "INF", "USD", "Done"), ("Non-EVM", "BSOL", "USD", "Done"),
        ("EVM", "USDHL", "USD", "Done"), ("EVM", "USDH", "USD", "Done"), ("EVM", "MUSD", "USD", "Done"),
        ("EVM", "THBILL", "USD", "Done"), ("EVM", "SFRXUSD", "USD", "Done"), ("EVM", "WSTHYPE", "USD", "Done"),
        ("EVM", "FEUSD", "USD", "Done"), ("EVM", "BTCB", "USD", "Done"), ("Non-EVM", "USX", "USD", "Done"),
        ("EVM", "MKR", "USD", "Done"), ("EVM", "SUSDE", "USD", "Done"), ("EVM", "USDE", "USD", "Done"),
        ("EVM", "PYUSD", "USD", "Done"), ("EVM", "WBTC", "USD", "Done"), ("Solana", "PSTUSDC", "USD", "Done"),
        ("Solana", "ONYC", "USD", "Done"), ("EVM", "DAI", "USD", "Done"), ("non-EVM", "JUPUSD", "USD", "Done")
    ]

    # Generate multi-column table
    pdf.set_font("helvetica", "B", 8)
    pdf.set_fill_color(220, 220, 220)
    pdf.cell(30, 7, "Chain", 1, 0, 'C', True)
    pdf.cell(35, 7, "Base", 1, 0, 'C', True)
    pdf.cell(35, 7, "Quote", 1, 0, 'C', True)
    pdf.cell(30, 7, "Status", 1, 1, 'C', True)
    
    pdf.set_font("helvetica", "", 8)
    for row in coverage_data:
        if pdf.get_y() > 260:
            pdf.add_page()
            pdf.set_font("helvetica", "B", 8)
            pdf.cell(30, 7, "Chain", 1, 0, 'C', True)
            pdf.cell(35, 7, "Base", 1, 0, 'C', True)
            pdf.cell(35, 7, "Quote", 1, 0, 'C', True)
            pdf.cell(30, 7, "Status", 1, 1, 'C', True)
            pdf.set_font("helvetica", "", 8)
        
        pdf.cell(30, 6, row[0], 1, 0, 'C')
        pdf.cell(35, 6, row[1], 1, 0, 'C')
        pdf.cell(35, 6, row[2], 1, 0, 'C')
        pdf.cell(30, 6, row[3], 1, 1, 'C')

    pdf.output("docs/pdf/Blocksize_State_Coverage.pdf")
    print("Generated: Blocksize_State_Coverage.pdf")

if __name__ == "__main__":
    generate_docs()
    generate_catalog()
    generate_flow()
    generate_pricing()
    generate_agent_manual()
    generate_state_coverage()
