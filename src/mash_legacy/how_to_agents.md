Most agents are horrible at integrating with domain-specific knowledge and adapting to feedback. I know most people hear this and think, "Great, I'll just slap a rag solution on top." But that's not enough. You can add retrieval, memory, and 3 or 4 other nifty engineering techniques on top of each other, and it still won’t be enough.
I’ve owned the agent infra for agents used by millions of people, and am now building continually learning, self-improving agents for businesses. Feedback loops are an inevitability that EVERY agent builder needs to embrace.
This is everything I've learned from building agents that have TANGIBLY improved over the course of a few weeks to a few months.

PART 1: Prepare your system
Step 0: What’s your agent’s end goal?
Stop thinking "let's just get a prototype out and see what happens."
You have to define what your agent's goal is. What business metric is it lifting? Are you trying to boost retention? Are you trying to maximize conversions? Are you trying to push usage up? Whatever it is, you need to make sure that you're focused on that first because it will trickle down. If your agent is not satisfying some real business outcome, there's no way to justify its existence.
Building this agent is going to be hard. It's going to take time, it's going to be a long improvement cycle, and you are going to invest time and money. So if you don't have a plan to show some actual results, then it's a waste of time.
I would suggest focusing on a core thesis for your agent. Is it

    User stays engaged for the most possible amount of time?
    User queries the most possible tools you have?
    User’s intended goal is achieved as fast and quickly as possible?

Consumer chat apps fall into the first bucket, utilitarian / prosumer into the second, and customer support / success into a mix of the second and third.
Depending on what you pick, the next steps will be vastly different, so think very carefully about this.
Step 1: Think in signals
The more you can think in terms of performance outcomes, the better your agent is going to be. Just a thumbs up or thumbs down on a message is a lazy way to collect signals on whether responses are good or bad, but I see it on literally every human-facing agent out there.

If you're building something user-facing, there are HUNDREDS of different signals you should be paying attention to.
Your signals don’t have to be tied to a single chat. You can do day-long or week-long signals (num chats opened per day, num chats opened per week, avg weekly response time, etc.), and extract more long tail gains, but this depends on whether or not you expect users to typically stay on your product for weeks or months on end.
Some signals I’ve measured before from a SINGLE message for engagement maxing are

    Measuring the time between agent response -> user response
    Binary label of whether the user left the chat or not
    What follow-up question was clicked
    User sentiment label (positive, negative, neutral)

There is such a thing as a ‘bad’ signal or too little signal. You can't just focus on one or two signals and then launch, because then your agent is going to be hyper-tuned for that particular signal optimization.
For example, let’s say you define a signal to measure response time. If your feedback loop implementation is halfway decent, your agent will always spit out the most negative or sycophantic messages possible, or just really simple one or two-word messages. It’s a problem traditionally known as reward hacking or reward overoptimization.
Users will churn because you’re not paying attention to whether or not user sentiment is positive or negative over a week or two. Purely negative sentiment over 1 week may mean all users get annoyed and leave.
So the reality is you should be continuously thinking about new signals and how your existing signals are performing. That’s the real and most important job of an agent builder.
Once you’ve defined your signal list, it’s time to improve your UI.
Step 2: Define a UI that makes signal collection easy
The single highest lever here is whether or not you can collect good signals is by defining an interface that can collect feedback easily. There's a reason why Cursor is so successful, and it's purely because they were able to integrate user feedback seamlessly with the tab feature. Anyone can and should adopt this mental model.
If you're building a basic chatbot, add a simple follow-up selector. That’s a signal.
If you’re building an internal search agent for enterprises, add hyperlinks + link tracking to the output so you can track what links were clicked and which ones weren’t.
If you’re building a coding agent, good luck competing with Claude (lmao).
Jokes aside, your UI should be well-suited for this signal collection process, because it’s definitely going to bottleneck you if you don’t.
PART 2: Build your feedback loops
Step 3: Simple, signal-derived rankers
The highest lift thing you can do for response improvement is feed in examples. I’ve tried system prompt changing, subagent architectures, etc., and by far the highest lift came from good few-shot examples during generation time. And now, you should have a set of signals that are tuned to specific values on a PER RESPONSE basis.
This is what most people do:
Image
But THIS is what most people should be doing:
Image
This feedback loop ensures that no matter what happens, you’re always prioritizing the conversations that fulfill your business needs the most.
As you add new signals, play around with different thresholds, the number of examples fed in (a good rule of thumb is 10 examples at most), and weighing different signals if certain ones matter more than others. This is a great starting point that will get you ahead of 99% of agent deployments.
Step 4: Run experiments
Ideally, every single change you make to your output logic should be run through an experiment, so you know whether or not your change actually moved the needle on some end business metric. Depending on what your usage is, experiments can run anywhere from a few days to a few weeks.
In the past I’ve waited for around 50k user messages sent per control group (for a b2c product), but this can vary depending on how much usage you have. I would suggest posthog or mixpanel, but you can use whatever you’d like.
The anatomy of an experiment is simple, just set up

    Control group (no changes)
    Experiment group (whatever system prompt, or new signal change you made)
    Business metric tracking (whatever your signals are optimizing towards)

If you’re making a change in your agent ranking and you haven’t verified it works against a control group, you might as well be throwing darts against a dartboard.
Step 5: Let Claude code do work for you
Here’s a prompt you can steal to build this feedback loop into whatever agent you already have:
```
# Add Production Feedback Loops to My Agent
I need you to analyze my codebase and implement a complete feedback loop system that will make my agent improve over time based on real user interactions.
## Step 1: Understand My Agent & Business Goals
First, analyze my codebase and ask me:
1. **What business metric is this agent trying to improve?**
- User retention/engagement?
- Task completion speed?
- Conversion rates?
- Query success rate?
- Something else?
2. **What's the core thesis for this agent?**
- Maximize user engagement time?
- Maximize tool usage across available features?
- Achieve user goals as quickly as possible?
3. **How do users currently interact with this agent?**
- What does a typical conversation flow look like?
- Where does the agent succeed or fail?
- What feedback mechanisms exist today (if any)?
Based on my answers, help me think through what truly matters for my specific use case.
## Step 2: Design Signal Collection Strategy
Now work with me to identify 5-10 meaningful signals we should track. Guide me through thinking about:
**Per-message signals** that indicate response quality:
- Time between agent response → user response
- Whether a user abandoned the conversation
- User sentiment (positive/negative/neutral)
- Which suggested actions/links/options were clicked
- Message length or complexity
- Follow-up question type
**Session-level signals** for longer-term quality:
- Total conversation length
- Number of tool calls made
- Task completion (yes/no)
- User returned within 24 hours
**Long-tail signals** for week/month patterns:
- Conversations per week
- Average response time over time
- Retention metrics
For each signal, help me understand:
- **Why it matters** for my business goal
- **How to collect it** given my existing UI
- **What good vs bad looks like** (threshold values)
- **Potential failure modes** (how the agent might game this signal)
Make sure we have a balanced set of signals so the agent doesn't over-optimize for one dimension.
## Step 3: Implement Signal Collection
Analyze my codebase and implement:
1. **Signal capture hooks** integrated into my existing agent code
2. **Storage layer** for signals + conversations (use whatever makes sense - SQLite, Postgres, files, etc.)
3. **Schema design** that links signals to specific messages/conversations
4. **Validation logic** to ensure signal quality
Make this as non-invasive as possible to my existing code.
## Step 4: Enhance UI for Passive Signal Collection
Look at my current user interface and suggest 3-5 UI improvements that make signal collection hands-off:
- For chatbots: Add follow-up question buttons, quick reaction emojis, suggested next actions
- For search/research agents: Add link tracking, "helpful/not helpful" on each result
- For coding agents: Track which suggestions are accepted/rejected/modified
- For task agents: Add completion confirmation, difficulty rating
Then implement these UI changes in my codebase.
## Step 5: Build Signal-Derived Response Ranker
This is the core improvement system. Implement:
1. **Conversation storage**: Save all past conversations with their signal scores
2. **Embedding generation**: Create semantic embeddings for each historical conversation
3. **Ranking system** that scores past responses by:
```
final_score = semantic_similarity * signal_quality_score
```
Where signal_quality_score combines multiple signals with weights we define together
4. **Retrieval logic** that:
- Takes the current user query
- Finds the top 10 most similar past conversations
- Filters for ones with high signal scores
- Returns best examples
5. **Prompt injection**: Automatically inject these top examples into the agent's prompt at generation time
Show me how to configure signal weights and similarity thresholds.
## Step 6: Add Experimentation Framework
Implement a simple A/B testing system:
1. **Traffic splitting**: Randomly assign users/sessions to control or experiment groups
2. **Variant configuration**: Easy way to test different:
- Signal weights
- Number of examples injected
- Ranking thresholds
- System prompt changes
- Any other parameter
3. **Metrics tracking**: Log all defined signals for both groups
4. **Analysis helpers**: Functions to compare performance between groups with statistical significance
Make it trivial to run experiments on any change.
## Step 7: Monitoring & Iteration Tools
Build dashboards/scripts to:
- View signal distributions over time
- Identify which historical examples get retrieved most
- Track experiment results
- Debug bad responses
- Monitor for signal gaming or unexpected patterns
## Requirements
- **Production-ready code**: Handle errors, add logging, make it robust
- **Minimal dependencies**: Use what's already in my stack when possible
- **Clear documentation**: Explain how everything works and how to use it
- **Modular design**: Easy to swap components or add new signals later
- **Performance**: Don't slow down my agent's response time
## Deliverables
Provide:
1. **Complete implementation** integrated into my existing codebase
2. **Migration guide** if database changes are needed
3. **Configuration file** for defining signals, weights, and thresholds
4. **Deployment instructions** for production
5. **Runbook** for running experiments and interpreting results
6. **Monitoring setup** to track system health
## Let's Start
Analyze my codebase, then ask me the questions from Step 1. Once you understand my goals, we'll work through each step together to build a feedback loop that makes my agent genuinely improve over time.
```
TLDR
Define the business outcomes. Retention? Specific feature activation? User maximum happiness? Likelihood to recommend to another person?
Think in signals. What signals represent progress towards achieving the business outcome that you care about?
Build a ranking system that factors in signals to properly reference the most important examples at generation time
Experiment. Test around with different signals, architectures, and model to get the best bang for your buck out of your agent.
Excellent, agentic systems are not vibe coded, they’re precisely engineered to improve over time. 