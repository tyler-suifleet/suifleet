// Proof-only type declaration from the pinned compiler fork. No arithmetic
// functions or axioms are supplied; executable release sources remain unchanged.
module std::integer;

#[mode(spec), ext(spec_only)]
native public struct Integer has copy, drop, store;
