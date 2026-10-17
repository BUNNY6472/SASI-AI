Project Eris
Solver Guidebook
Everything you need to solve a challenge the right way, and the rules that keep it fair for everyone else too.

How to Use This Doc
This doc does two jobs at once. The first part tells you how to actually approach an Eris challenge. The second part is the rulebook, what's allowed, what's not, and where the grey areas are.
One thing to keep in mind throughout: if a specific challenge's own instructions say something different from what's written here, the challenge's instructions win. This guidebook is the default, not the override.

1. Welcome to Project Eris
1.1 What Eris Is Trying to Measure
While you're building a solution, keep this in mind: at some stage, it's going to be used to train SOTA models and agents. So your solution genuinely needs real ML training or fine-tuning in it. Inference-only solutions don't count here.
And if you've found something genuinely insightful in the dataset, some real pattern or trick, don't just hardcode it. Train a model to find and use that insight instead. Because whatever your solution does, an agent is going to learn from it later. If it sees you training a model to discover something, it learns the concept, the reasoning, and how to apply it elsewhere. If it just sees a hardcoded value, it learns nothing, it has no idea where that number came from or why.
Here's a simple way to think about it. Say two people solve the same problem:

1. Using HPO (Hyperparameter Optimization) inside the submission script, to actually search for and find the best set of parameters.
2. Hardcoding the best parameters they found earlier, offline, on their own machine.

Looking at solution 1, the agent genuinely learns what HPO is, why it matters, and how to do it properly. Looking at solution 2, it learns nothing at all. It can't find any pattern like "on this kind of challenge, use these parameters", because there isn't one to find. Sounds harsh, but that's really how it is, and that's exactly why we're strict about this.

1.2 Solving the Challenge, Not Chasing the Leaderboard
Quick heads up on how the platform works: it's built just like Kaggle. There's a Public LB and a Private LB, and like most of you probably already know, the Private LB is the one that actually decides the final rankings and prizes. Not the Public one.
Here's the catch though, the Private LB stays hidden for the entire competition and only gets revealed at the very end. So all you see while the challenge is running is the Public LB. Don't take that score, or your rank on it, as the true measure of how well your solution generalizes. If you over-optimize for the Public LB blindly, your final rank can drop pretty drastically once the Private LB comes out. So use your Public LB rank as a soft signal of how you're doing, not as proof of your solution's true generalizability.

2. Guide to Solving an Eris Challenge
The platform works a lot like Kaggle, and you're brought on here assuming you already have some solid Kaggle experience. So honestly, solving a challenge should already feel familiar. Here's the flow, kept short and direct:

1. You pick up a challenge.
2. Open that challenge's solution page, then download the data from there.
3. Read through the overview provided. Use that, along with the data, to build your solution, then come back once you've got something ready to submit.

On submitting: you'll first submit a CSV file, and separately, the solution script that runs end to end and generates that submission.csv itself. That first CSV step might feel redundant, but it's built this way on purpose. It lets you run your solution locally, then upload just the CSV to instantly check your score, instead of submitting the full script and waiting for it to run every single time.
Important: the CSV you upload yourself is only for your own checking. It doesn't count for review or for the leaderboard at all. The leaderboard only goes off the submission.csv generated when your submitted script actually runs on the test environment.
After that, it's Kaggle again: a Public LB you can check anytime, and a Private LB that decides the actual prizes but only gets revealed once the competition ends. See Section 1.2 for the full context on how to treat those two.
One more thing: if you ever see an unbelievably high score sitting on the Public LB, don't let it get in your head. Don't panic and start drifting into grey-area territory just to keep up with it. Remember, every solution only gets reviewed after the competition ends, so that suspiciously high score might just be a rule violation that hasn't been caught yet. Keep improving your own solution as much as you genuinely can, and don't worry about scores that don't add up.

3. What Happens When You Submit
Every time you submit a solution script, it goes through four checks. The first three look at your files before anything runs. The fourth is the actual run on the test environment. Knowing what each one looks for saves you credits, and saves you a lot of waiting.

3.1 The Four Checks

1. CSV grading check. The submission.csv you upload is passed to the challenge's grading script, to confirm it grades without errors. Wrong columns, missing ids, or badly formatted values fail here.
2. Prompt compliance check. An LLM reads your solution against the challenge's overview, and checks that it does what the challenge expects and follows every rule written there.
3. Determinism check. An LLM reads your script and checks three things: that your random seeds are set correctly, that nothing in the script depends on how long it has been running, and that nothing depends on the machine it's running on. To be clear about what this isn't: we don't run your script twice and compare the results. It's a code review, not a rerun. Section 3.3 covers how to pass it.
4. Script run. Your script runs end to end on the test environment. Any error during the run fails the submission, whether that's an out-of-memory error, a ValueError, a missing package, or anything else.

Checks 1 to 3 run together and report together. Your script only reaches check 4 if all three pass.
Passing the prompt compliance check doesn't mean your solution is accepted. Think of it as a bouncer at the door: it turns away most of the solutions that clearly break the rules, so reviewers only spend their time on the shortlisted ones, since a human review costs a lot more. Every solution that gets through is still reviewed by a reviewer after the competition ends, and it can still be rejected then.
If the prompt compliance check fails your solution, fix the script. Don't resubmit it unchanged and hope for a different answer.

3.2 Which Failures Cost You a Credit
Only check 4 consumes a submission credit. Once your script starts running on the test environment and fails there, that credit is gone and it won't be returned. The one exception is a server crash, covered in Section 4.6.
If you fail check 1, 2 or 3, you get your credit back, and you can submit again after a 5-minute cooldown.
One thing that trips people up: check 1 looks at the CSV you upload. The submission.csv your script writes on the test environment is a different file. If that one comes out malformed or incomplete, the credit is consumed like any other run, see Section 4.6.

3.3 Passing the Determinism Check
The check wants a script that would do exactly the same thing on every run, on any machine. Here's what it rejects:

* Missing seeds. Every source of randomness needs a fixed seed: Python's random, NumPy, PyTorch on both CPU and GPU, and the seed or random_state argument of every model, data split, and sampler you use.
* Runtime-conditioned code. Anything that reads the clock to make a decision: time budgets, stopping training after a set number of minutes, or picking the number of epochs from how fast the first one ran. Fix your epochs or steps in advance instead, see Section 4.4.
* Test-environment-conditioned code. Anything that adapts to the machine it finds itself on: os.cpu_count(), torch.cuda.device_count(), "cuda if available, otherwise cpu", or a batch size picked from free GPU memory. Write those values in as constants.
* try/except fallbacks. A fallback that switches to a different method when something fails, like trying one model and quietly loading another one on an exception.
* Unused switches and branches. Environment-variable switches, debug flags, and if-branches that choose between methods, even when only one path ever actually runs. Keep one fixed path through the script.

And here's what's fine, because it depends only on your data and your seed, never on the clock or the machine:

* HPO inside the script, with a fixed seed and a fixed number of trials, not a time budget.
* Early stopping on your validation score.
* Mixed precision, AMP or bf16.
* A DataLoader with multiple workers, as long as the worker count is a fixed number and the loader is seeded, like in the template below.

3.4 A Seed Template You Can Copy
Put this at the top of your script, before you build any model or DataLoader. It's written for PyTorch. Swap in your own dataset and batch size, and keep everything else as it is.

```python
import random
import numpy as np
import torch

SEED = 42
NUM_WORKERS = 4
DEVICE = torch.device("cuda")

random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
torch.cuda.manual_seed_all(SEED)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False


def seed_worker(worker_id):
    worker_seed = torch.initial_seed() % 2**32
    random.seed(worker_seed)
    np.random.seed(worker_seed)


loader_generator = torch.Generator()
loader_generator.manual_seed(SEED)

train_loader = torch.utils.data.DataLoader(
    train_dataset,
    batch_size=32,
    shuffle=True,
    num_workers=NUM_WORKERS,
    worker_init_fn=seed_worker,
    generator=loader_generator,
)
```

If you use other libraries, pass the same SEED to them too: random_state in scikit-learn models and splits, and the seed argument in LightGBM, XGBoost, CatBoost, and the Hugging Face Trainer.

4. Environment & Allowed Libraries
4.1 What You Actually Have Access To
The test environment runs on the Kaggle Docker image. So whatever libraries are available in that image are exactly what's available to your submitted solution scripts, nothing more.
Using any library outside of that list is strictly prohibited, and will not be allowed under any circumstances. There's no approval route for this either, so don't ask a reviewer to sign off on an extra package, and don't install one from inside your script.

4.2 What You Can Use the Internet For
To be clear, the packages you do have access to are totally free to use the internet, for downloading model weights from timm, Hugging Face, and similar sources. That part is completely fine.

4.3 What's Never Okay
Don't try to install models through any other means, GitHub or otherwise. Any attempt like that will be immediately rejected.
We know GitHub hosts plenty of legitimate research models that never make it onto timm or Hugging Face. That's not really why it's banned. The real issue is that GitHub releases almost always come bundled with their own custom library or codebase to actually use the model, and since you can only use libraries that are already preinstalled in the test environment (Section 4.1), that custom code isn't something you're allowed to install anyway. So in practice, GitHub-hosted models are ruled out, not because of where they're hosted, but because using them properly would mean installing something outside the allowed library list.

4.4 Time Limits
The expected max runtime for any solution, in any challenge, in any domain, is 1.5 hours.
We don't put a hard gate that instantly rejects your solution the moment it crosses that 1.5 hour mark, that's on purpose. We genuinely want you spending your energy on the quality and novelty of your solution, not clock-watching. So if a solution is genuinely good, we do allow it to run over, by up to 30 minutes extra. Not more than that though, we have full rights to reject anything that runs past 1.5 hours by more than 30 minutes.
In practice, we usually only end up allowing 5 to 10 minutes of extra time, the full 30-minute grace only kicks in under certain conditions.
One thing we always ask solvers to do: fix the length of your training in advance. Pick a fixed number of epochs or steps that you've measured to finish comfortably inside the limit, with room left over for inference and writing the submission file. Don't read the clock inside your script, and don't stop training based on elapsed time. A time-based stop makes your result depend on how fast the machine happens to run that day, and the determinism check at submission will reject it (see Section 3.3).

4.5 Compute
The test environment runs on an Nvidia A10G GPU. That's the hardware every solution gets trained and evaluated against, so plan your training schedule and time budget (Section 4.4) around it.
There's no blanket model-size or VRAM cap beyond that, if it fits on the A10G, it fits. That said, some challenges set their own model-size limits in the challenge description. If a challenge does that, follow it, challenge-specific limits like this override this general guidebook (see Section 5.4).

4.6 Submission Limits
There are two separate submission limits running at the same time, and they interact in a way that trips people up, so read this carefully.
The first is a local, per-competition limit: you get 6 submission credits for that specific competition, and one extra credit gets added back every 4 hours.
The second is a global limit: a total cap on submissions across every competition you're participating in, not challenge-specific. Right now that's somewhere around 15 to 25 submissions a day, but honestly, this number shifts depending on how many competitions are live at once, the day of the week, and a few other factors we can't promise to always flag when they change. Treat that range as a guide, not a guarantee.
Here's the part that catches people out: your local, per-competition credits are useless if you've already burned through your global daily credits. Having credits left on a specific challenge doesn't mean anything if the global limit says you're done for now.
Also, unlike Kaggle, where all your daily submissions reset together at the start of a new day, the global credit here comes back individually, 24 hours after you used it. So if you make a submission today at 4 PM that uses your last global credit, that specific credit comes back 24 hours later, at 4 PM the next day, not at midnight, not at the start of a new day. Keep that in mind when you're planning submissions close to a deadline.
The local CSV check-submission from Section 2, the one you upload just to instantly see your score, doesn't cost you anything. It doesn't touch either credit pool, local or global. Only the real end-to-end script submission, the one that actually runs on the test environment, consumes a credit.
If your script fails when it actually runs on the test environment, that credit is gone either way, even if the failure wasn't really about your solution's quality. The one exception: every once in a while, the underlying Amazon server itself crashes, which has nothing to do with your script. If that happens, report it to us and we'll rerun it, though we can't promise a turnaround time on that. Honestly, if you want a faster fix, just submit again instead of waiting, these crashes are random and rare, so a resubmission usually clears it faster than waiting on us.
One more thing worth knowing: if your script actually runs and produces a submission.csv, but that file turns out malformed or incomplete, say some rows are missing because of a silent bug, the credit is still consumed, same as any other run. But you won't get a partial score either. You'll see an error on the leaderboard instead of a score, and you'll be placed dead last, even below someone who scored a flat 0. A broken output is worse than a bad one, so it's worth double-checking your submission.csv is actually complete before you burn a credit on it.

4.7 One Independent, End-to-End Script
Every submission script runs on its own, individually, on the test environment. There's no way to attach a pre-built dataset or artifact to it like you can on Kaggle. So every script has to be fully independent and end to end: preprocessing, feature engineering, training, and inference all have to happen inside that one script, every single time it runs, starting fresh from the raw data given to you.
This also means training and ensembling multiple models inside that same single script is completely fine, that's still one end-to-end run. What's not fine is caching outputs from a previous run (like precomputed embeddings) and quietly loading them back in on a later submission, there's no previous run to load anything from in the first place.

5. General Solver Rules
5.1 Clearly Allowed
These are safe to use across any Eris challenge, no reviewer approval needed.

1. Loading pretrained backbone or general-purpose weights (from timm, Hugging Face, and similar sources) directly inside your submission script, as covered in Section 4.2. That's just using a starting point, not the same as bringing in your own already fine-tuned model.
2. Real training or fine-tuning happening entirely inside the submission script, including things like HPO, as already covered in Section 1.1. If the learning is genuinely happening at submission time, you're doing it right.
3. Inference-time techniques that would work exactly the same way in genuine real-world production use, where your model only ever sees one test sample (or one small batch, the way batch normalization operates) at a time, with zero visibility into the rest of the test set. TTA (Test-Time Augmentation) is fine, it only ever touches one sample at a time. Batch normalization recalculated at the batch level is fine too, same as it would work in production batch inference.
4. Sharing your solution or approach publicly on the #general channel on our Discord server. This is actually encouraged, especially if you're a challenge creator. It only raises the bar for everyone's solutions.

5.2 Clearly Not Allowed
These will get your solution rejected, no exceptions.

1. Solving the challenge with pure algorithmic or rule-based logic instead of real ML, even if it technically works. If there's no actual training or fine-tuning happening, it doesn't count here, however clever the trick is.
2. Training or fine-tuning a model outside the submission script, then publishing those weights somewhere (like Hugging Face) and loading your own self-hosted, already-fine-tuned weights back in. All training and fine-tuning has to happen inside the submission script itself. You can only load general-purpose backbone weights, not weights you've already fine-tuned elsewhere.
3. Bringing in external datasets for any model, for any challenge. Only the data given to you in the challenge (plus pretrained backbone weights, per 5.1) can be used.
4. Sharing solutions, code, or approaches internally between solvers working on the same challenge. To be clear, this is different from posting publicly on the #general Discord channel, that's fine (see 5.1), it's the internal, one-on-one sharing that's not allowed.
5. Using the test set for anything beyond genuine one-sample (or one-batch) inference. Pseudo-labelling, reweighting training samples using test data, test-time adaptation, and calibrating your model's outputs using the test set's overall distribution are all not allowed, even though you never get test labels in the first place. It's not about labels, it's about realism: if a technique needs visibility into the whole test set up front, it's not something that would hold up in real-world production inference, and it just lets your solution quietly adapt to this one test set instead of being genuinely generalizable.
6. Generating your own synthetic data and training on it. It's a genuinely legitimate ML technique elsewhere, and we're not saying it doesn't work, but it's not allowed here. The whole point of a challenge is to see how much your solution can genuinely earn using only the amount of data we've actually given you.

5.3 Grey-Area Techniques
These are some of the grey-area methods that should only be used when the solver accepts the risk that the solution may be rejected.
Regex and TF-IDF, below, are just the two named examples. If you're weighing up some other technique that isn't listed here, here's the general test to run on yourself: strip the ML model out of your solution entirely. If it still basically works, it's not compliant. The whole point, all the way back in Section 1.1, is that the model has to be the one doing the learning, not you, working around it.
One more thing worth knowing: we've seen challenges whose own rules explicitly say a technique like this is allowed, n-grams and Markov chains, for instance. That's fine as a starting point, but it doesn't take the technique out of grey area. It just means you won't get rejected purely for using it. Reviewers can still reject your solution if it turns out to be over-relying on this dataset's specific distribution instead of genuinely generalizing, same as anywhere else in this guidebook.

5.3.1 Using Regex in Your Solutions (Especially in NLP Competitions)
Using regex itself is not a violation. But keep in mind, over-relying on regex and similar techniques makes the solution too fragile. It stops being robust or generalizable. So use regex as little as possible. Using it for cleaning the dataset, or for finding something perfectly deterministic like numbers, is fine. But if we catch that your solution is over-relying on regex instead of ML, like exploiting how the data was generated or overfitting to the underlying distribution of the data, we will reject it.

5.3.2 Using TF-IDF, N-grams, Markov Chains, and Similar Statistical Techniques
TF-IDF, n-grams, Markov chains, and similar statistical or frequency-based techniques all fall into this same grey area, for the same underlying reason: they're all fundamentally dependent on this specific dataset's distribution, not genuinely generalizable the way a trained model is.
It's entirely up to the reviewer of that competition whether they want to allow any of these or not. Also note that the reviewer has full rights to allow the usage of one of these techniques for one participant, and not for another, within the same competition. It mainly depends on two factors: the quality and novelty of your solution, and how much it's relying on the technique. So only use them if you're okay with those rules.

5.4 Challenge-Specific Rules
Some challenges restrict you from using certain kinds of models, or explicitly limit you to specific ones. When that happens, you have to stay compliant with those rules, challenge-specific restrictions like this genuinely do override this general guidebook.
That said, if a challenge's description also throws in things like "no internet access", "decoding tricks are fine", or "fully rule-based solutions are okay", ignore those. They're not real, intentional overrides, just leftover boilerplate that didn't get cleaned up, and they contradict what's already established in this guidebook (see Sections 4.2 and 5.2).
If you're ever genuinely unsure whether something in a challenge's description is a real restriction or just leftover boilerplate, don't guess and gamble on an appeal later. Ask a reviewer first, and wait for their answer before you submit.
On runtime specifically: if a challenge's own rules mention an expected max runtime that's under 1.5 hours, say 30 minutes, you do have to follow that, though this is a rare case. But keep in mind, the overall max runtime for any solution, in any challenge, stays at 1.5 hours regardless, see Section 4.4 for the full rule.

5.5 How Solutions Are Reviewed: It's Relative
One thing worth understanding before you submit: the compliance and appropriateness review of solutions is relative. We don't have a complete list of every possible technique that we check a solution against, and then approve or reject it on that basis. The exception is the unambiguous cases. A solution that clearly has a lot of hardcoded values or labels, pulls from GitHub or other outside sources, or leans on an exploit instead of a genuine ML approach gets removed outright, no comparison needed (see Section 5.2). Everything else is judged on a relative basis.
What we do instead is rate the appropriateness of every solution within a competition, and then compare those ratings against each other, going down the leaderboard one pair at a time: rank n against rank n+1. Take rank 1 and rank 2 as the example. Say the rank 1 solution is slightly less appropriate than the rank 2 solution. If their appropriateness scores are nearly identical, rank 1 won't get rejected over that small gap. But if the appropriateness score drops drastically at rank 1 compared to rank 2, we remove rank 1. The same comparison then applies to rank 2 against rank 3, and so on down the leaderboard.
Every reviewer and Principal Reviewer (PR) conducts this a little differently, so don't read that example as an exact formula. It's there to give you a good idea of what relative means here.

5.6 Appealing a Rejection
If your solution gets rejected, you can appeal it. Tag and inform the reviewer of your solution on your private channel on our Discord server, and explain in detail why you think it shouldn't have been rejected.
One important timing note: every solution only gets reviewed, and possibly rejected, after the competition ends. So you won't find out if something was against the rules until then. And no submission credit, local or global, ever gets refunded, whether the rejection stands or you win an appeal. Credits are consumed the moment you use them, that's final regardless of the eventual outcome.

5.7 Accounts and Team Solving
One account per person. Running multiple accounts is completely banned, and if we catch it, that's an immediate ban, no warning.
Team solving isn't a thing here either, it's not implemented on the platform, and it's not allowed even informally. That said, sharing your approach publicly on the #general Discord channel is both allowed and actively encouraged, as covered in Section 5.1, especially right now, since most solvers are still struggling to cross the AI baseline. Public sharing there helps everyone, it's just the private, one-on-one collaboration that's off the table.

6. Domain-Specific Guidelines
6.1 NLP / Seq-to-Seq Challenges
Most of what matters here is already covered in Section 5.3, Grey-Area Techniques, especially the regex and TF-IDF points. Those are the two you'll be most tempted to reach for on an NLP challenge, so it's worth re-reading them before you start.
One more thing specific to NLP challenges, and honestly to a good chunk of Eris challenges in general: some of these datasets are synthetically generated. And synthetic data, just by nature of how it's generated, can end up with a very synthetic structure, sometimes exploitable structure.
Noticing that structure isn't the problem. Gaming it directly is. So if you spot something like that, you can't just write a regex or build a prebuilt template around it and call it a day. Whatever pattern or shortcut you've found has to be discovered and used by the model itself, as part of training, same as we said all the way back in Section 1.1. Not hardcoded by you, outside of it. If the insight didn't come from the model actually learning it, it doesn't count.

6.2 Computer Vision and Object Detection Challenges
Same synthetic-dataset possibility applies here too, and tabular models absolutely love exploiting it. So to be clear, your solution cannot use tabular models directly on raw pixels. If we find that, it gets rejected immediately, no discussion.
Hand-engineered features built off the images, things like color histograms or edge counts, are a bit different. Feed them into a real deep learning model alongside the actual image, and that's completely fine, they're just extra signal for a genuine CV model. But if those same features are only ever used by a tabular model, with no real image model in the loop at all, that's grey area, not an automatic ban, but it makes the solution fragile, and it might get rejected for exactly that reason.
Your solution has to actually be robust and generalizable. That means training a real CV model from scratch, or fine-tuning one, think CNNs, ViTs, that kind of thing. Not a tabular model dressed up to look like it's doing computer vision.
One nuance worth calling out: a tabular model trained on top of embeddings from a pretrained backbone is a different story, that one falls under grey area, not an automatic ban. You can submit it, but go in knowing the reviewer might still reject it if it turns out to be non-robust or non-generalizable.

6.3 RAG and Retrieval Challenges
This is the second highest-violation domain we see, right after computer vision. What we always want here is a genuinely trained ranking model, and more often than not, that means an actual deep learning model doing the ranking.
What people do instead, a lot: they hand-engineer features out of the data, and then run a classic ranking algorithm on top of those features. That's not enough. Feature engineering plus an off-the-shelf ranking algorithm isn't the same as a model that's actually learned to rank, and it won't hold up here.
The retrieval step itself is a different story, a frozen, off-the-shelf embedding model for retrieval is fine, same logic as using a pretrained backbone elsewhere in this guidebook. It's the ranking or generation part sitting on top of that retrieval that has to be genuinely trained.
To be clear though, inference-only solutions are not allowed here, same as everywhere else in this guidebook. A frozen retriever is fine, but a major portion of your overall solution still has to be a genuinely trained or fine-tuned model. We'd honestly recommend fine-tuning the retriever too if you can, it's not required, but a fully frozen pipeline end to end won't fly.

6.4 Fine-tuning Challenges
Here you have to make sure you're actually fine-tuning a pretrained backbone. A lot of people quietly skip that part, they take a pretrained model, pull out its embeddings, and just slap a tabular model on top instead.
On its own, that's not really fine-tuning, and it falls into grey area here, same treatment as the equivalent case in Section 6.2 for CV. It's not an automatic ban, but the reviewer can and will reject it if the solution turns out non-robust, non-generalizable, or looks like it's just dodging genuine fine-tuning.
Chemistry and biology challenges get a specific carve-out here, but it depends on how the challenge is categorized. If it's categorized as an NLP challenge (Section 6.1), tabular models on top of frozen embeddings are simply fine, no grey area about it. But if it's specifically categorized as a Fine-tuning challenge, that leniency goes away entirely: you have to genuinely fine-tune a domain-appropriate model, something like ChemBERT, for instance, no matter what. XGBoost, LightGBM, and similar tabular models on their own won't be tolerated here, deep learning is mandatory. If you've got extra hand-engineered features, you're free to feed them in as additional inputs to the model's head, that's fine. Ensembling your fine-tuned model with something else falls under grey area too, and it'll only get accepted if the performance and scores are genuinely strong, that's the reviewer's call.

6.5 From-Scratch Challenges
As the name suggests, here you're training a model from scratch. Using any pretrained model, for embedding retrieval, knowledge distillation, or anything else, is not allowed. Your training has to genuinely start from scratch, and on the provided data alone.
Same time limit as everywhere else applies here too, there's no extension for training from scratch. You still get the 1.5 hours, plus up to 30 minutes of grace for a genuinely good solution, laid out in Section 4.4. Plan your architecture and training schedule with that budget in mind from the start.
On tokenizers, BPE, and similar auxiliary tools: these aren't automatically banned. Whether they're allowed is usually spelled out in the challenge description itself. If the challenge doesn't mention it either way, we don't count these as "pretrained models," standard tooling like that is fine by default. That said, reviewers still have full rights to reject anything that ends up giving your solution an unfair edge over a genuinely from-scratch trained model, even something as small as a tokenizer. So if you want a real guarantee before you commit to an approach, either confirm with a reviewer first, or just don't use anything pretrained at all.

6.6 Biology, Chemistry, and Other Domains Without Their Own Section
Not every domain has its own subsection here yet, biology, chemistry, and others like them don't have a dedicated slot. When a challenge falls into one of these, it usually just gets bucketed under whichever category fits best, sometimes NLP (Section 6.1), sometimes Fine-tuning (Section 6.4), depending on what the challenge is actually asking you to do.
One thing worth knowing if you land on one of these: things like regex, TF-IDF-style vectorizers, or even tabular models, the ones that sit strictly in Section 5.3's grey area, or get flagged in Section 6.2 for CV, tend to be relatively more rule-compliant here. Domains like biology and chemistry often have real structure to them, molecular formulas, sequences, and so on, that makes these techniques a genuinely natural fit, not just a shortcut around doing real ML.
But relatively rule-compliant doesn't mean automatically accepted. It's still fully up to the reviewer's judgment, and using them here is no guarantee your solution gets through.
One exception to that general leniency: if a bio/chem challenge is specifically categorized as a Fine-tuning challenge, Section 6.4's stricter rule takes over instead, genuine fine-tuning of a domain-appropriate model is required there, no matter the domain.

7. Challenge Reviews
This section is for challenge creators. It's about how your challenge gets reviewed, not your solution. Solution reviews are covered in Section 5.5.
7.1 How the Review Queue Works
As soon as you submit your challenge, it enters a queue. It works on a first come, first served basis: whoever submitted first gets reviewed first. Tagging or messaging Reviewers and Principal Reviewers doesn't move you up the list. There is no estimated review time. Solving the live challenges makes the queue move faster.
