#!/usr/bin/env python
# -*- coding: utf-8 -*-

import math

from .solution import Solution
from .components import RandomInput

# The maximum acceptable rate of precognitive solutions being marked as non-precognitive
# Lowering this increases the number of runs non-precog solutions require
# We could probably be more lax on this one since precogs are submitted much less often, but it only saves about
# 10 runs when checking a typical non-precog production solution
MAX_FALSE_NEGATIVE_RATE = 0.0001  # 1 in 10,000
# The maximum acceptable rate of non-precognitive solutions being marked as precognitive
# Lowering this increases the number of runs precog solutions require
# This is the expensive one, but non-precogs are more common so we need a low false positive rate for them
MAX_FALSE_POSITIVE_RATE = 0.0001  # 1 in 10,000

# A minimum amount of allowed runtime we prescribe so that our classification accuracy remains at acceptable levels for
# solutions within any reasonable max_cycles limit, and only degrades for bogosort solutions (assuming the caller
# has set max_cycles high enough to allow).
MIN_PRECOG_CHECK_RUN_CYCLES = 2_000_000  # Large enough to ensure it doesn't constrain typical required run counts

# Fully-precog solutions will only succeed for 1 in hundreds or more seeds.
# Finding enough success seeds to be confident that their missing variants can't be bad luck can thus take thousands of
# runs, and since distinguishing high failure rates caused by precog vs by allowable assumptions (such as certain
# sequences of inputs never appearing) is a Hard problem, for now we'll cop out and distinguish them by the extremity
# of their fail rate, hard-capping the maximum number of runs we think a solution should ever need.
# This mainly serves to improve the runtime of fully-precog solutions.
# TODO: Do something more elegant. Setting a probabilistic threshold for the similarity of successful run variants to
#       each other would slightly speed up precogs small enough to still succeed every 100-200 seeds, but would choke on
#       production-scale precogs where you'll never see a second success before hitting runtime constraints.
MAX_RUNS = 500


def binary_int_search(f, low=0, high=1):
    """Given a boolean function f(n) of the form g(n) >= c, where g is monotonically increasing, use binary search to
    find the minimum natural value of n that solves the equation.
    """
    # Init: double high until we find an upper bound
    while not f(high):
        high *= 2

    while high != low + 1:
        mid = (high + low) // 2
        if f(mid):
            high = mid
        else:
            low = mid

    return high


def is_precognitive(solution: Solution, max_cycles=None, just_run_cycle_count=0, verbose=False):
    """Given a Solution, run/validate it then check if fits the current community definition of a precognitive solution.

    Currently, this means a solution which assumes the value of the Nth molecule of a random input, for some N >= 2.
    Stated conversely, a solution is non-precognitive if for each random input I, each N >= 2, and each type of molecule
    M that I may create, there exists a random seed where the Nth input of I is M, and the solution succeeds.

    In practice we check this with the following process:
    1. Run the solution on the original level, verifying it succeeds (validate the solution's expected score here too if
       possible). Track how many molecules were generated from each random input (call this N), and what the nth
       molecule's variant was for every n up to N.
    2. Increment the seeds of all random inputs at once until a set of seeds is found that has the same first molecule
       produced for each input (since assumptions on the first input are allowed). Incrementing the seeds in lockstep
       also ensures assumptions about inputs that share a seed are not violated (such assumptions are not forbidden in
       non-precognitive solutions).
    3. Repeat step 1 with the new random seed(s) (but without requiring that it succeed). Update N for each random input
       to be whichever run used less molecules (since the solution might always terminate on a certain molecule, using
       the max would risk the last n never being able to 'find' all variants).
       If the solution succeeds, aggregrate the nth-input-variant data with that of the first run, in order to track
       which variants for a given n have had at least one run succeed.
       If the solution fails, put the same nth-input-variant data in a separate 'failure variants' dataset.
    4. Repeat steps 2-3 until either:
       * the dataset of succeeding runs covers every possible variant of every possible n (2 <= n <= N), for all random
         input components. If so, we can stop and declare the solution non-precognitive.
       * A resource-bounded upper limit on the number of runs is reached (for example, grossly high cycle count
         solutions take a long time to run so might only be allowed to run a few times).
         If this limit is reached, stop and check if we found an nth-molecule-variant that never appeared in a
         successful run, but did appear in any failing run. If so, declare the solution non-precognitive.
         Otherwise, if we had missing nth-molecule-variants but we never managed to see them in failing runs either,
         declare the solution non-precognitive. For example, the first input assumption might make some variants
         impossible to find in the first bucket, or a very long-running solution might succeed every run we give it
         but take too long to cover all variants.

    Args:
        solution: The loaded solution to check.
        max_cycles: The maximum cycle count allowed for a SINGLE run of the solution (passed to Solution.run).
            Note that this is not the total number of cycles allowed across all runs; any solution within this limit
            is allowed to run at least twice, with the maximum runs taken being limited for extremely slow solutions.
            This is bounded to 300 runs, since at a certain point it can be assumed that the missing variants are not
            being found because the first molecule was unique in its bucket.
        just_run_cycle_count: In order to save on excess runs, if the solution has just been successfully run on the
            loaded level (and not been modified or reset() since), pass its cycle count here to skip the first run (but
            still pull the first run's data from the Solution object).
        verbose: If true, print more detailed info on the number of runs performed and how many passed before returning.
    """
    # Same default as Solution.run
    if max_cycles is None:
        if solution.expected_score is not None:
            max_cycles = 2 * solution.expected_score.cycles
        else:
            max_cycles = Solution.DEFAULT_MAX_CYCLES

    # Based on the single-run max cycles value, calculate a rough max cycle count for the sum of all runs, such that
    # solutions that are within the max_cycles limit will be able to run at least twice
    # Keep it to at least 2,000,000 cycles since for values lower than that, solutions that are well within the
    # max_cycles limit will tend to not be given enough runs to converge
    max_run_cycles = max(2 * max_cycles, MIN_PRECOG_CHECK_RUN_CYCLES)
    max_runs = MAX_RUNS  # Will be updated after we've checked the first run's cycle count below

    # Hang onto references to each random input in the solution
    random_inputs = [input_component for input_component in solution.inputs
                     if isinstance(input_component, RandomInput)]

    if not random_inputs:
        return False  # duh

    # For each input, N is the minimum molecules the solution must use from that input
    # Before we do any checks that require resetting the input objects, initialize Ns to the data from the last run if
    # just_run_cycle_count was provided
    Ns = [random_input.num_inputs if just_run_cycle_count else math.inf for random_input in random_inputs]

    # Collect a bunch of information about each random input which we'll use for calculating how many runs are needed
    num_variants = [len(random_input.molecules) for random_input in random_inputs]
    first_input_variants = [random_input.reset().get_next_molecule_idx() for random_input in random_inputs]
    bucket_sizes = [sum(random_input.input_counts) for random_input in random_inputs]
    rare_counts = [min(random_input.input_counts) for random_input in random_inputs]  # Bucket's rarest molecule count
    # Rarest molecule count in first bucket, accounting for the fixed first molecule
    # and ignoring now-impossible variants (since they can't fail a run)
    first_bucket_rare_counts = [min(true_count for variant, count in enumerate(random_input.input_counts)
                                    if (true_count := (count - 1 if variant == first_variant else count))
                                       != 0)
                                for random_input, first_variant in zip(random_inputs, first_input_variants)]

    # Calculate the min number of runs seemingly non-precog solutions must take, before we're confident an
    # illegally-assumed molecule would have had its failing variant show up by now (i.e. we haven't false-negatived
    # a precog solution).
    # This is based on re-arranging (where n is the index of a molecule the solution illegally assumes):
    # P(nth input has any untested variant)
    # <= (1 - rarest_variant_chance)^runs
    # <= MAX_FALSE_NEGATIVE_RATE
    min_runs = 1
    for i, random_input in enumerate(random_inputs):
        # Since we only check seeds with same first molecule as the base seed to respect the first input assumption,
        # molecules from the first bucket may end up with a rarer variant. Use the worst case (ignoring cases that
        # become impossible in the first bucket since those can also never cause a run to fail)
        rarest_variant_chance = min(rare_counts[i] / bucket_sizes[i],
                                    first_bucket_rare_counts[i] / (bucket_sizes[i] - 1))

        min_runs = max(min_runs, math.ceil(math.log(MAX_FALSE_NEGATIVE_RATE)
                                           / math.log(1 - rarest_variant_chance)))

    # For each random input, keep a list of sets containing all variants that appeared for the i-th input of that
    # random input. We don't store the 1st input's variants so store a dummy value at the front to keep our indices sane
    success_run_variants = [[set()] for _ in range(len(random_inputs))]
    fail_run_variants = [[set()] for _ in range(len(random_inputs))]
    num_runs = 0
    num_passing_runs = 0
    while num_runs < max_runs:
        solution.reset()  # Reset the solution from any prior run (this also picks up seed changes)

        # Skip to the next seed(s) if the first molecule of each random input isn't the same as in the original input
        if any(random_input.get_next_molecule_idx() != first_input_variants[i]
               for i, random_input in enumerate(random_inputs)):
            for random_input in random_inputs:
                random_input.seed += 1
            continue

        # Reset the inputs after that molecule check
        for random_input in random_inputs:
            random_input.reset()

        # Run the solution with this seed of the input, checking if it succeeds (ignoring the exact score)
        try:
            # Run the solution
            if num_runs == 0:
                # if just_run_cycle_count was given, skip the first run to save time and assume the solution object is
                # in the correct post-run state
                cycles = just_run_cycle_count if just_run_cycle_count else solution.run(max_cycles=max_cycles).cycles

                # Limit the max runs based on the solution's cycle count and our time constraints
                # Note that max_runs is guaranteed 2 or more here since max_run_cycles is > 2 x max_cycles
                # More commonly this value will be excessively large, but max_success_runs (calculated after min_runs
                # is reached) serves as the more practical constraint on number of runs taken
                max_runs = min(max_runs, max_run_cycles // cycles)  # TODO: Might also want // reactors here
            else:
                solution.run(max_cycles=max_cycles)

            # Check how many molecules the solution consumed for each random input, and lower each N if possible
            # Note that if just_run_cycle_count was provided, we initialized N already and reset the solution,
            # so we skip in that case
            if not (num_runs == 0 and just_run_cycle_count):
                for i, random_input in enumerate(random_inputs):
                    # Ignore molecules that only made it into the pipe since their variant can't affect the solution
                    this_N = random_input.num_inputs - sum(1 for mol in random_input.out_pipe if mol is not None)
                    Ns[i] = min(Ns[i], this_N)

            target_variants_data = success_run_variants
            num_variants_to_store = Ns  # Direct reference is safe since we only read from this

            num_passing_runs += 1
        except Exception as e:
            if num_runs == 0:
                # Not allowed to crash on the original seed, otherwise do nothing
                raise Exception(f"Error in base seed: {type(e).__name__}: {e}")

            target_variants_data = fail_run_variants  # The data set that this run's variants should be added to
            # Make sure we don't store data on variants of molecules from after the solution crashed
            num_variants_to_store = [random_input.num_inputs - sum(1 for mol in random_input.out_pipe if mol is not None)
                                     for random_input in random_inputs]

        num_runs += 1

        # Track all nth input variants that appeared in this run for 2 <= n <= N
        for random_input, variants_data, num_new_variants in zip(random_inputs, target_variants_data, num_variants_to_store):
            random_input.reset().get_next_molecule_idx()  # Reset and skip past n = 0

            if num_new_variants > len(variants_data):
                variants_data.extend([set() for _ in range(num_new_variants - len(variants_data))])

            for n in range(1, num_new_variants):
                variants_data[n].add(random_input.get_next_molecule_idx())

        if num_runs >= min_runs:
            # If we passed the minimum total runs to be sufficiently confident we aren't marking a precog solution as
            # non-precog, we can immediately report the solution as non-precog if there are no failing runs containing a
            # molecule variant we haven't seen succeed yet
            if all(n >= len(fail_run_variants[i])  # Note that fail_run_variants isn't guaranteed to be of length N
                   or not (fail_run_variants[i][n] - success_run_variants[i][n])
                   for i, N in enumerate(Ns)
                   for n in range(1, N)):
                if verbose:
                    print(f"No failing variants found for {sum(Ns)} input molecules ({num_passing_runs} / {num_runs} runs passed)")
                return False

            if num_runs == min_runs:
                # Once we've passed min_runs (and now that our N is probably plenty accurate):
                # Calculate the maximum number of *successful* runs seemingly precog solutions can take before we're
                # confident any missing molecule variants are never going to show up in a successful run (i.e. confident
                # we haven't false-positived)
                # This is based on:
                # P(false positive)
                # ~= P(any molecule is missing a success variant)
                # = 1 - P(no molecule missing success variant)
                # = 1 - P(single molecule has all success variants)^(N - 1)
                # = 1 - P(first bucket molecule has all success variants)^(bucket_size - 1)
                #       * P(normal bucket molecule has all success variants)^(N - bucket_size)
                # ... (and so on) ...
                # ~= 1 - [1 - (1 - (first_bucket_rare_count / bucket_size))^successful_runs]^(bucket_size - 1)
                #        * [1 - (1 - (regular_bucket_rare_count / bucket_size))^successful_runs]^(N - bucket_size)
                # <= MAX_FALSE_POSITIVE_RATE
                # With the first bucket bias accounting, this is too complex to solve exactly for successful_runs
                # unlike with the false negative equation, but since we know it's monotonically increasing, we can just
                # binary search to find the minimum valid successful_runs
                max_success_runs = 1
                for i, (bucket_size, N) in enumerate(zip(bucket_sizes, Ns)):
                    max_success_runs = max(max_success_runs, binary_int_search(
                        lambda success_runs: 1 -
                                             # First bucket
                                             (1 - (1 - (first_bucket_rare_counts[i] / (bucket_size - 1)))
                                                  ** success_runs)
                                             ** min(bucket_size - 1, N)  # N might be less than the bucket size
                                             # Remaining buckets
                                             * (1 - (1 - (rare_counts[i] / bucket_size))
                                                    ** success_runs)
                                               ** max(N - bucket_size, 0)  # N might be less than the bucket size
                                             <= MAX_FALSE_POSITIVE_RATE))

            if num_passing_runs >= max_success_runs:
                # Since we just checked that at least one missing variant is failing, if we've exceeded our maximum
                # successful runs, we're confident we aren't false-positiving and can mark the solution as precognitive
                if verbose:
                    # This is redundant but I want to report which molecule was precognitive
                    for i, N in enumerate(Ns):
                        for n in range(1, N):
                            # Check for any variants of the ith input that appeared in a failing run but never in a succeeding run
                            if n < len(fail_run_variants[i]) and fail_run_variants[i][n] - success_run_variants[i][n]:
                                print(f"Molecule {n + 1} / {N} appears to always fail for some variants"
                                      f" ({num_runs - num_passing_runs} / {num_runs} runs failed)")
                                return True

                return True

        # If for every random input, we've succeeded on all variants up to the minimum number of molecules the solution
        # needs from that input to complete, the solution is guaranteed non-precog
        if all(len(success_run_variants[i][n]) == num_variants[i]
               for i, N in enumerate(Ns)
               for n in range(1, N)):
            if verbose:
                print(f"Successful variants found for all input molecules ({num_passing_runs} / {num_runs} runs passed)")
            return False

        # Increment the random seed(s)
        for random_input in random_inputs:
            random_input.seed += 1

    # If we escaped the loop without returning and max_runs was constrained by the solution cycle count rather than
    # the MAX_RUNS constant, warn the user that we've been time-constrained. For typical cycle counts (e.g. < 10k) this
    # limit will never be encountered before the other probabilistic run count exit points.
    if verbose and max_runs != MAX_RUNS:
        print("Warning: Precog check terminated early due to time constraints; check accuracy may be reduced.")

    # If we reached our time-constrained run limit but never found all variants in successful runs, consider the
    # solution precog if any of the missing variants appeared in any failing run
    for i, N in enumerate(Ns):
        for n in range(1, N):
            # Check for any variants of the ith input that appeared in a failing run but never in a succeeding run
            if n < len(fail_run_variants[i]) and fail_run_variants[i][n] - success_run_variants[i][n]:
                if verbose:
                    print(f"Molecule {n + 1} / {N} appears to always fail for some variants"
                          f" ({num_runs - num_passing_runs} / {num_runs} runs failed)")
                return True

    # If none of the missing variants showed up in any of the failing runs, we have no evidence that the solution
    # hardcodes against that variant, so report the solution as non-precognitive
    if verbose:
        print(f"No failing variants found for {sum(Ns)} input molecules ({num_passing_runs} / {num_runs} runs passed)")
    return False
