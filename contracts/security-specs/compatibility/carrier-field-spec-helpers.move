module sf050_native::carrier_field_specs;
#[mode(spec), ext(spec(prove, target = sf050_native::carrier_fields::rewrap))]
fun raw_rewrap_spec(ctx: &mut TxContext) {
    sf050_native::carrier_fields::rewrap(ctx);
}
