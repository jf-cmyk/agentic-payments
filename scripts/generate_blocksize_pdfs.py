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

    def pricing_table(self, rows, headers=("Access path", "Availability", "Terms")):
        self.set_font("helvetica", "B", 9)
        self.set_fill_color(220, 220, 220)
        self.cell(60, 8, headers[0], 1, 0, 'C', True)
        self.cell(50, 8, headers[1], 1, 0, 'C', True)
        self.cell(70, 8, headers[2], 1, 1, 'C', True)
        
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

    pdf.section_title("3. Authentication Modes")
    pdf.body_text(
        "- Direct HTTP: use an official signed x402 v2 PAYMENT-SIGNATURE.\n"
        "- Connectors: sign in through the supported OAuth flow to use an eligible "
        "starter-credit allowance. Caller-selected identity headers do not grant "
        "production credits."
    )

    pdf.output("docs/pdf/Blocksize_API_Documentation.pdf")
    print("Generated: Blocksize_API_Documentation.pdf")

def generate_catalog():
    pdf = BlocksizePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.chapter_title("Institutional Data Catalog")
    
    catalog = [
        ("Crypto VWAP", "Core", "2.0-4.0 Credits", "BTC/USD, ETH/USD, SOL/USD, and supported long-tail pairs"),
        ("Crypto Bid/Ask", "Core", "2.0-4.0 Credits", "Shared bid/ask namespace for supported crypto symbols"),
        ("FX Spot Rates", "Standard", "5.0 Credits", "Currently enabled FX pairs such as EUR/USD"),
        ("Precious Metals", "Standard", "5.0 Credits", "XAU, XAG, XPT, XPD, and copper tickers"),
        ("Discovery and Docs", "Free", "0 Credits", "Search, instrument lists, pricing, prompt examples, and support docs")
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
    pdf.body_text(
        "1. Agent requests /v1/vwap/BTC-USD.\n"
        "2. Server returns 402 Payment Required.\n"
        "3. PAYMENT-REQUIRED lists the exact amount, asset, network, and resource.\n"
        "4. An official x402 v2 client signs the selected authorization.\n"
        "5. Agent retries the exact request with PAYMENT-SIGNATURE."
    )
    
    pdf.section_title("2. Deterministic Unlock")
    pdf.body_text(
        "After facilitator verification, successful settlement, and durable local "
        "finalization, the gateway returns the paid JSON payload and prevents proof "
        "replay. Authenticated connectors can separately use an eligible starter allowance."
    )
    
    pdf.output("docs/pdf/Blocksize_User_Flow.pdf")
    print("Generated: Blocksize_User_Flow.pdf")

def generate_pricing():
    pdf = BlocksizePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.chapter_title("Institutional Pricing Guide")
    
    pdf.section_title("1. Direct x402 Unit Pricing")
    pdf.body_text(
        "Public paid HTTP routes use direct x402 per request. Raw-data defaults range "
        "from 0.002 USDC for core crypto to 0.008 USDC for supported equity bid/ask; "
        "packaged workflow prices can differ. Always use the live 402 challenge as "
        "the authoritative price and network list."
    )

    pdf.section_title("2. Production Access Paths")
    paths = [
        ("Direct x402", "Public HTTP", "Live route price"),
        ("Connector credits", "Eligible users", "Starter allowance"),
        ("Account plan", "Contact sales", "Agreed terms"),
    ]
    pdf.pricing_table(paths)

    pdf.section_title("3. Account Plans")
    pdf.body_text(
        "Self-serve purchase routes are not exposed in production. Teams that need "
        "sustained authenticated access should contact "
        "Blocksize to discuss an account plan."
    )
    
    pdf.output("docs/pdf/Blocksize_Pricing_Guide.pdf")
    print("Generated: Blocksize_Pricing_Guide.pdf")

def generate_agent_manual():
    pdf = BlocksizePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.chapter_title("Agent Integration Guide")
    
    pdf.section_title("1. Access Requirements")
    pdf.body_text(
        "Direct HTTP clients use an official signed x402 v2 payment flow. Eligible "
        "OpenAI, Claude, and Cursor connector users authenticate through OAuth to use "
        "a starter allowance. Raw wallet and caller-selected identity headers do not "
        "grant production credits."
    )
    
    pdf.section_title("2. Automated Discovery")
    pdf.body_text("Fetch the MCP Discovery Manifest at /mcp/manifest.json to receive full tool definitions and JSON-schema parameters.")
    
    pdf.section_title("3. HTTP Integration")
    pdf.body_text(
        "Request a paid route, parse its PAYMENT-REQUIRED challenge with an official "
        "x402 v2 client, then retry the exact request with PAYMENT-SIGNATURE. For "
        "sustained authenticated access, contact Blocksize about an account plan."
    )
    
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
