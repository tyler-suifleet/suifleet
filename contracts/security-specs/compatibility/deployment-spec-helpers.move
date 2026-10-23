    #[mode(spec), ext(spec(prove, target = new_receipts))]
    fun new_receipts_security_spec(): Receipts {
        let receipts = new_receipts();
        prover::prover::ensures(receipts.version == RECEIPTS_VERSION);
        prover::prover::ensures(receipts.statuses.is_empty());
        receipts
    }

    #[mode(spec), ext(spec(prove, target = receipts_mut))]
    fun receipts_mut_security_spec(deployment: &mut DeploymentRecord): &mut Receipts {
        let present = df::exists(&deployment.id, ReceiptsKey {});
        prover::prover::asserts(!present || df::exists_with_type<ReceiptsKey, Receipts>(&deployment.id, ReceiptsKey {}));
        let old_version = if (present) df::borrow<ReceiptsKey, Receipts>(&deployment.id, ReceiptsKey {}).version else 0;
        prover::prover::asserts(old_version <= RECEIPTS_VERSION);
        let result = receipts_mut(deployment);
        prover::prover::ensures(result.version == RECEIPTS_VERSION);
        if (!present || old_version < RECEIPTS_VERSION) prover::prover::ensures(result.statuses.is_empty());
        result
    }

    #[mode(spec), ext(spec_only, pure)]
    fun spec_hex_byte(byte: &u8): bool {
        (*byte >= 48 && *byte <= 57) || (*byte >= 97 && *byte <= 102)
    }

    #[mode(spec), ext(spec_only(loop_inv(target = assert_recipe_hash)), pure)]
    fun assert_recipe_hash_loop(bytes: &vector<u8>, i: u64): bool {
        i <= bytes.length() && prover::vector_iter::all_range!(bytes, 0, i, |byte| spec_hex_byte(byte))
    }

    #[mode(spec), ext(spec(prove, target = assert_recipe_hash))]
    fun assert_recipe_hash_security_spec(recipe_sha256: &String) {
        let bytes = recipe_sha256.as_bytes();
        prover::prover::asserts(bytes.length() == 64);
        prover::prover::asserts(prover::vector_iter::all!(bytes, |byte| spec_hex_byte(byte)));
        assert_recipe_hash(recipe_sha256);
    }

    #[mode(spec), ext(spec(prove, target = new_deployment_verified))]
    fun new_deployment_verified_security_spec(
        component_name: String, version: String, recipe_blob_id: String,
        artifact_sha256: String, recipe_sha256: String, target_groups: vector<ID>,
        target_devices: vector<ID>, clock: &Clock, ctx: &mut TxContext,
    ): DeploymentRecord {
        prover::prover::asserts(recipe_sha256.as_bytes().length() == 64);
        prover::prover::asserts(prover::vector_iter::all!(recipe_sha256.as_bytes(), |byte| spec_hex_byte(byte)));
        prover::prover::asserts(!target_groups.is_empty() || !target_devices.is_empty());
        let artifact = artifact_sha256;
        let recipe = recipe_sha256;
        let blob = recipe_blob_id;
        let sender = ctx.sender();
        let record = new_deployment_verified(component_name, version, recipe_blob_id,
            artifact_sha256, recipe_sha256, target_groups, target_devices, clock, ctx);
        prover::prover::ensures(record.artifact_sha256 == artifact);
        prover::prover::ensures(record.recipe_blob_id == blob);
        let typed = df::exists_with_type<RecipeDigestKey, String>(&record.id, RecipeDigestKey {});
        prover::prover::ensures(typed);
        if (typed) prover::prover::ensures(*df::borrow<RecipeDigestKey, String>(&record.id, RecipeDigestKey {}) == recipe);
        prover::prover::ensures(record.creator == sender);
        record
    }

    #[mode(spec), ext(spec(prove, target = handover))]
    fun deployment_handover_security_spec(d: &mut DeploymentRecord, recipient: address, ctx: &TxContext) {
        prover::prover::asserts(ctx.sender() == d.creator);
        prover::prover::asserts(d.schema_version != 2);
        let artifact = d.artifact_sha256;
        let blob = d.recipe_blob_id;
        handover(d, recipient, ctx);
        prover::prover::ensures(d.creator == recipient);
        prover::prover::ensures(d.artifact_sha256 == artifact);
        prover::prover::ensures(d.recipe_blob_id == blob);
    }

    #[mode(spec), ext(spec(prove, no_opaque, target = make_deployment))]
    fun make_deployment_security_spec(creator: address, component_name: String,
        version: String, recipe_blob_id: String, artifact_sha256: String,
        target_groups: vector<ID>, target_devices: vector<ID>, clock: &Clock,
        ctx: &mut TxContext): DeploymentRecord {
        prover::prover::asserts(!target_groups.is_empty() || !target_devices.is_empty());
        let artifact = artifact_sha256;
        let blob = recipe_blob_id;
        let record = make_deployment(creator, component_name, version, recipe_blob_id,
            artifact_sha256, target_groups, target_devices, clock, ctx);
        prover::prover::ensures(record.creator == creator);
        prover::prover::ensures(record.artifact_sha256 == artifact);
        prover::prover::ensures(record.recipe_blob_id == blob);
        prover::prover::ensures(df::exists_with_type<ReceiptsKey, Receipts>(&record.id, ReceiptsKey {}));
        record
    }

    #[mode(spec), ext(spec(prove, target = report_status))]
    fun report_status_security_spec(_deployment: &mut DeploymentRecord, _cap: &DeviceCap, _status: u8, _ctx: &TxContext) {
        prover::prover::asserts(false);
        report_status(_deployment, _cap, _status, _ctx);
    }

    #[mode(spec), ext(spec(prove, target = report_status_for_group))]
    fun report_status_for_group_security_spec(_deployment: &mut DeploymentRecord, _cap: &DeviceCap,
                                             _group: &DeviceGroup, _status: u8, _ctx: &TxContext) {
        prover::prover::asserts(false);
        report_status_for_group(_deployment, _cap, _group, _status, _ctx);
    }
