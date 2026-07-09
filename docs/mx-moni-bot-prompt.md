# MX Moni Bot Prompt

Use the prompt below directly with bots running on DO.

Before using this prompt, also read:

- `docs/stockbot-code-map.md`
- `/opt/stockbot/docs/stockbot-code-map.md`

## Prompt

```text
You can use the mx-moni skill to manage and inspect a Chinese A-share mock trading account.

Goal:
Show me all the practical ways to query and operate the mock trading account, with concrete examples.

Please do the following in English:

1. Explain what kinds of account actions are supported by mx-moni.
2. List all major query methods, including:
   - account balance
   - current positions
   - open/past orders
   - trade-related status checks
   - cancel order / cancel all
   - buy / sell
   - post-trade summary posting
3. For each method, provide:
   - what it does
   - when to use it
   - one natural-language example command
   - one direct API-style description if applicable
4. Clearly distinguish:
   - query actions
   - trading actions
   - cancellation actions
   - posting / summary actions
5. Include example user requests such as:
   - "Show my account balance"
   - "Show my current positions"
   - "Show my orders"
   - "Buy 600519 at 1700 for 100 shares"
   - "Sell 000001 at market for 500 shares"
   - "Cancel order 261030200000048829"
   - "Cancel all pending orders"
   - "Post a summary of today's operations"
6. Mention any important input rules:
   - A-share stock codes must be 6 digits
   - quantity should be in shares
   - market-price vs limit-price behavior
   - cancel order needs a valid order ID
7. Mention safety constraints:
   - this is for mock trading, not real-money trading
   - do not give investment advice
   - only explain how to use the account operations
8. End with a compact cheat sheet table summarizing:
   - action name
   - sample command
   - expected result

If possible, organize the answer cleanly with sections and concise examples.
Do not be vague. Be operational and concrete.

After explaining the methods, try the safe read-only queries first:
- show account balance
- show current positions
- show orders

Do not place any buy/sell order unless explicitly instructed.
```

## Suggested Read Path On DO

`/opt/stockbot/docs/mx-moni-bot-prompt.md`
