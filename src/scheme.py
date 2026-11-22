"""What is specific to Dilithium2 outside its input distributions."""
Q = 8380417
PREFIX = "pqcrystals_dilithium2_ref_"


def short_name(function):
    return function.removeprefix(PREFIX)


def full_name(function, known):
    """`function` as it appears in `known`, with or without the prefix."""
    return function if function in known else PREFIX + function
