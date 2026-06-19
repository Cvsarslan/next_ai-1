frappe.provide("nextai.cache");

$(document).ready(function () {
    setTimeout(()=>{
        if (
            (
                frappe.user.has_role("NextAI User") ||
                frappe.session.user === "Administrator" ||
                frappe.user.has_role("System Manager")
            ) &&
            !isRestrictedDoctype()
        ) {
            nextAIFeature();
        }
    }, 2000)
});

function isRestrictedDoctype() {
    const route = frappe.get_route(); // ["Form", "DocType", "Role"]
    if (!route || route[0] !== "Form") return false;

    const doctype = route[1];
    return (doctype === "DocType" || doctype === "Customize Form");
}

function nextAIFeature(){
    const allowedTypes = ['Text', 'Text Editor', 'Small Text', 'Long Text', 'JSON', 'HTML Editor', 'Markdown Editor', 'Code'];

    $(document).on('click', 'input[data-fieldtype], textarea[data-fieldtype], .ace_editor, .ql-editor', function (e) {
        let $input = $(this);
        let fieldtype = $input.data('fieldtype') || $input.closest('[data-fieldtype]').data('fieldtype');

        if (!allowedTypes.includes(fieldtype)) return;

        $('.apiIcon').remove();
        $('[data-has-icon]').each(function () {
            $(this).css('padding-right', '').removeAttr('data-has-icon');
        });

        if (!$input.parent().hasClass('input-wrapper')) {
            $input.wrap('<div class="input-wrapper"></div>');
        }

        let $container;
        if ($input.hasClass('ace_editor')) {
            $container = $input;
        } else if ($input.closest('.ace_editor').length) {
            $container = $input.closest('.ace_editor');
        } else if ($input.hasClass('ql-editor') || $input.closest('.ql-editor').length) {
            $container = $input.closest('.ql-container');
        } else {
            $container = $input;
        }

        $container.attr('data-has-icon', true);

        const $icon = $('<button/>', {
            class: 'apiIcon',
            type: 'button',
            html: `
                <div style="
                    background: white;
                    border-radius: 9999px;
                    padding: 3px 10px;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    gap: 6px;
                ">
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="14" fill="currentColor" class="bi bi-stars" viewBox="0 0 16 16">
                        <path d="M7.657 6.247c.11-.33.576-.33.686 0l.645 1.937a2.89 2.89 0 0 0 1.829 1.828l1.936.645c.33.11.33.576 0 .686l-1.937.645a2.89 2.89 0 0 0-1.828 1.829l-.645 1.936a.361.361 0 0 1-.686 0l-.645-1.937a2.89 2.89 0 0 0-1.828-1.828l-1.937-.645a.361.361 0 0 1 0-.686l1.937-.645a2.89 2.89 0 0 0 1.828-1.828zM3.794 1.148a.217.217 0 0 1 .412 0l.387 1.162c.173.518.579.924 1.097 1.097l1.162.387a.217.217 0 0 1 0 .412l-1.162.387A1.73 1.73 0 0 0 4.593 5.69l-.387 1.162a.217.217 0 0 1-.412 0L3.407 5.69A1.73 1.73 0 0 0 2.31 4.593l-1.162-.387a.217.217 0 0 1 0-.412l1.162-.387A1.73 1.73 0 0 0 3.407 2.31zM10.863.099a.145.145 0 0 1 .274 0l.258.774c.115.346.386.617.732.732l.774.258a.145.145 0 0 1 0 .274l-.774.258a1.16 1.16 0 0 0-.732.732l-.258.774a.145.145 0 0 1-.274 0l-.258-.774a1.16 1.16 0 0 0-.732-.732L9.1 2.137a.145.145 0 0 1 0-.274l.774-.258c.346-.115.617-.386.732-.732z"/>
                    </svg>
                    <span style="font-size: 14px; font-weight: 500; color: #333;">NextAI</span>
                </div>
            `,
            style: `
                background: linear-gradient(to right, #00f0ff, #a000ff);
                border-radius: 9999px;
                padding: 2px;
                border: none;
                cursor: pointer;
                position: absolute;
                bottom: 10px;
                right: 10px;
                z-index: 1000;
                box-shadow: 0 2px 6px rgba(0,0,0,0.2);
                transition: transform 0.2s ease;
            `
        }).hover(
            function() { $(this).css('transform', 'scale(1.05)'); },
            function() { $(this).css('transform', 'scale(1)'); }
        );

        if (!$container.parent().hasClass('input-wrapper')) {
            $container.wrap('<div class="input-wrapper" style="position: relative;"></div>');
        } else {
            $container.parent().css('position', 'relative');
        }

        $container.parent().append($icon);

        $icon.on('click', async function () {
            let keyValue = {};

            let doctype = "";
            if (frappe.get_route()[0] === "Form") {
                doctype = frappe.get_route()[1];
            } else if (cur_dialog) {
                doctype = cur_dialog.doc.doctype;
            }


            const $fieldWrapper = $container.closest('[data-fieldname]');
            const fieldname = $fieldWrapper.length ? $fieldWrapper.data('fieldname') : 'unknown';
            const fieldtype = $fieldWrapper.length ? $fieldWrapper.data('fieldtype') : 'unknown';

            if ($container.hasClass('ace_editor')) {
                const aceEditor = ace.edit($container[0]);
                keyValue = {
                    key: fieldname,
                    value: aceEditor.getValue(),
                    doctype: doctype,
                    type: fieldtype
                };

            } else if ($container.hasClass('ql-container')) {
                const $qlEditor = $container.find('.ql-editor');
                keyValue = {
                    key: fieldname,
                    value: $qlEditor.html(),
                    doctype: doctype,
                    type: fieldtype
                };

            } else {
                keyValue = {
                    key: $input.attr('id') || $input.data('fieldname') || 'unknown',
                    value: $input.val(),
                    doctype: doctype,
                    type: fieldtype
                };
            }

            cloned = structuredClone(keyValue)
            delete cloned['value']
            req = JSON.stringify(cloned)

            if ((nextai.cache[req] != undefined) && (nextai.cache[req] == keyValue['value'])){
                frappe.msgprint('⚠️ Invalid Input: Cannot process AI-generated output as a new prompt. Please enter your own input.')
            } else {
                nextai.cache = {};
                try {
                    const res = await makeApiCall(keyValue);

                    $icon.prop('disabled', true).css('opacity', 0.6).css('pointer-events', 'none');

                    typeText(
                        $container.hasClass('ace_editor') ? $container :
                        $container.hasClass('ql-container') ? $container :
                        $input,
                        res,
                        $container.hasClass('ace_editor') ? 'ace' :
                        $container.hasClass('ql-container') ? 'quill' : 'input',
                        () => {
                            $input.trigger('change');
                        }
                    );

                } catch (err) {
                    console.error("API call failed:", err);
                } finally {
                    setTimeout(()=>{
                        $icon.prop('disabled', false).css('opacity', 1).css('pointer-events', 'auto');
                    }, 1000)
                }
            }

        });

        if ($input.hasClass('ace_editor')) {
            const editor = ace.edit($input[0]);
            editor.focus();
        } else {
            $input.focus();
        }
    });
}


function makeApiCall(data) {
    return new Promise((resolve, reject) => {
        frappe.call({
            method: 'next_ai.ai.get_ai_response',
            args: data,
            freeze:true, freeze_message:__("Connecting with NextAI"),
            callback: function (r) {
                if (r.message) {
                    const responseData = r.message.message;

                    delete data['value']
                    const cacheKey = JSON.stringify(data);
                    nextai.cache[cacheKey] = responseData;

                    resolve(responseData);
                } else {
                    reject("No message in response");
                }
            },
            error: function (err) {
                reject(err);
            }
        });
    });
}


function typeText($target, text, type, callback) {
    const totalDuration = 1000;
    const startTime = performance.now();
    const len = text.length;

    function step(now) {
        const elapsed = now - startTime;
        const progress = Math.min(1, elapsed / totalDuration);
        const charsToShow = Math.floor(progress * len);
        const partial = text.slice(0, charsToShow);

        if (type === 'ace') {
            const editor = ace.edit($target[0]);
            editor.setValue(partial, -1);
        } else if (type === 'quill') {
            const $editor = $target.find('.ql-editor');
            $editor.html(partial);
        } else {
            $target.val(partial);
        }

        if (progress < 1) {
            requestAnimationFrame(step);
        } else if (callback) {
            callback();
        }
    }

    requestAnimationFrame(step);
}

// -------------------------------------------------------------

let nextai_allowed_doctypes = null;

function loadNextAIAllowedDoctypes() {
    return new Promise((resolve) => {
        if (nextai_allowed_doctypes) {
            resolve(nextai_allowed_doctypes);
            return;
        }

        frappe.call({
            method: "frappe.client.get_list",
            args: {
                doctype: "NextAI Parsing",
                fields: ["doc"],
                filters: { enable: 1 }
            },
            callback(r) {
                nextai_allowed_doctypes = r.message?.map(d => d.doc) || [];
                console.log("NextAI Allowed Doctypes:", nextai_allowed_doctypes);
                resolve(nextai_allowed_doctypes);
            }
        });
    });
}

async function injectGlobalNextAIButton() {
    const route = frappe.get_route();
    if (!route || route[0] !== "Form") return;

    const frm = window.cur_frm;
    if (!frm) return;

    if (["DocType", "Customize Form"].includes(frm.doctype)) return;

    const allowed_doctypes = await loadNextAIAllowedDoctypes();
    if (!allowed_doctypes.includes(frm.doctype)) return;

    if (document.querySelector('#nextai-global-btn')) return;

    const header = document.querySelector('.page-head .page-title');
    if (!header) return;

    const $icon = $('<button/>', {
        id: 'nextai-global-btn',
        type: 'button',
        html: `
            <div style="
                background: white;
                border-radius: 9999px;
                padding: 3px 10px;
                display: flex;
                align-items: center;
                gap: 6px;
            ">
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="14"
                     fill="currentColor" viewBox="0 0 16 16">
                    <path d="M7.657 6.247c.11-.33.576-.33.686 0l.645 1.937a2.89
                    2.89 0 0 0 1.829 1.828l1.936.645c.33.11.33.576 0 .686
                    l-1.937.645a2.89 2.89 0 0 0-1.828 1.829l-.645 1.936
                    a.361.361 0 0 1-.686 0l-.645-1.937a2.89 2.89 0 0
                    0-1.828-1.828l-1.937-.645a.361.361 0 0 1 0-.686
                    l1.937-.645a2.89 2.89 0 0 0 1.828-1.828z"/>
                </svg>
                <span style="font-size:14px;font-weight:500;">Parsing</span>
            </div>
        `,
        style: `
            background: linear-gradient(to right, #00f0ff, #a000ff);
            border-radius: 9999px;
            padding: 2px;
            border: none;
            cursor: pointer;
            margin-left: 15px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.2);
        `
    });

    $icon.on('click', () => {
        const d = new frappe.ui.Dialog({
            title: "NextAI Prompt",
            size: "large",
            fields: [
                {
                    label: "Doctype",
                    fieldname: "doctype_name",
                    fieldtype: "Link",
                    options: "DocType",
                    read_only: 1,
                    default: frm.doctype
                },
                {
                    label: "Parsing",
                    fieldname: "parsing_name",
                    fieldtype: "Link",
                    options: "NextAI Parsing",
                    reqd: 1,
                    get_query() {
                        return {
                            filters: {
                                enable: 1,
                                doc: frm.doctype
                            }
                        };
                    }
                },
                { fieldtype: "Section Break" },
                {
                    label: "Your Input",
                    fieldname: "prompt",
                    fieldtype: "Long Text",
                    reqd: 1
                }
            ],
            primary_action_label: "Generate",
            primary_action(values) {
                frappe.call({
                    method: "next_ai.ai.parsing.get_ai_parser_response",
                    args: {
                        doctype: values.doctype_name,
                        name: values.parsing_name,
                        message: values.prompt
                    },
                    callback(res) {
                        const data = res.message?.message || {};
                        Object.keys(data).forEach(key => {
                            if (frm.get_field(key)) {
                                frm.set_value(key, data[key]);
                            }
                        });
                        frm.refresh_fields();
                    }
                });
                d.hide();
            }
        });
        d.show();
    });

    $(header).parent().append($icon);
}


frappe.router.on('change', () => {
    setTimeout(() => {
        $('#nextai-global-btn').remove();
        injectGlobalNextAIButton();
    }, 300);
});

$(document).ready(() => {
    setTimeout(injectGlobalNextAIButton, 500);
});


// ═══════════════════════════════════════════════════════════
//  Click 2 Click Consultant AI  —  Desk Chat Widget v3
// ═══════════════════════════════════════════════════════════
(function () {

const TODAY = new Date().toISOString().split('T')[0];

// ── Quick-action catalogue ───────────────────────────────
// Each action with `fields` shows a structured form (no AI quota used).
// Each action with only `prompt` sends the prompt via AI chat.
// Field keys: n=name, l=label, t=type, req=required,
//             opts=options (select), ldt=link_doctype (link),
//             def=default, ph=placeholder,
//             ct=childTable fieldname, cf=childField key
const QA = {
    sales: { label:'Sales', icon:'💼', color:'#00b4d8', actions:[
        { label:'New Customer', icon:'👤', btnLabel:'Create Customer',
          tool:'create_document', doctype:'Customer',
          fields:[
            { n:'customer_name',  l:'Customer Name',  t:'text',   req:1, ph:'e.g. ABC Corporation' },
            { n:'customer_type',  l:'Customer Type',  t:'select', req:1, opts:['Company','Individual'], def:'Company' },
            { n:'customer_group', l:'Customer Group', t:'link',   req:1, ldt:'Customer Group', ph:'Search group...' },
            { n:'territory',      l:'Territory',      t:'link',   req:0, ldt:'Territory',      ph:'Search territory...' },
        ]},
        { label:'Sales Invoice', icon:'🧾', btnLabel:'Create Invoice',
          tool:'create_document', doctype:'Sales Invoice',
          fields:[
            { n:'customer',      l:'Customer',      t:'link',   req:1, ldt:'Customer', ph:'Search customer...' },
            { n:'posting_date',  l:'Posting Date',  t:'date',   req:1, def:TODAY },
            { n:'_item_code', l:'Item Code', t:'link', req:1, ldt:'Item', ph:'Search item...', ct:'items', cf:'item_code' },
            { n:'_qty',       l:'Quantity',  t:'num',  req:1, def:'1',   ct:'items', cf:'qty' },
            { n:'_rate',      l:'Rate',      t:'num',  req:0, ph:'0.00', ct:'items', cf:'rate' },
        ]},
        { label:'Sales Order', icon:'📋', btnLabel:'Create Sales Order',
          tool:'create_document', doctype:'Sales Order',
          fields:[
            { n:'customer',      l:'Customer',      t:'link', req:1, ldt:'Customer', ph:'Search customer...' },
            { n:'delivery_date', l:'Delivery Date', t:'date', req:1, def:TODAY },
            { n:'_item_code', l:'Item Code', t:'link', req:1, ldt:'Item', ph:'Search item...', ct:'items', cf:'item_code' },
            { n:'_qty',       l:'Quantity',  t:'num',  req:1, def:'1',   ct:'items', cf:'qty' },
            { n:'_rate',      l:'Rate',      t:'num',  req:0, ph:'0.00', ct:'items', cf:'rate' },
        ]},
        { label:'Quotation', icon:'📑', btnLabel:'Create Quotation',
          tool:'create_document', doctype:'Quotation',
          fields:[
            { n:'quotation_to',     l:'Quotation To', t:'select', req:1, opts:['Customer','Lead'], def:'Customer' },
            { n:'party_name',       l:'Party Name',   t:'text',   req:1, ph:'Customer or Lead name' },
            { n:'transaction_date', l:'Date',         t:'date',   req:1, def:TODAY },
            { n:'_item_code', l:'Item Code', t:'link', req:1, ldt:'Item', ph:'Search item...', ct:'items', cf:'item_code' },
            { n:'_qty',       l:'Quantity',  t:'num',  req:1, def:'1',   ct:'items', cf:'qty' },
        ]},
        { label:'List Customers', icon:'📋', prompt:'List all customers' },
        { label:'List Invoices',  icon:'📋', prompt:'List recent sales invoices' },
    ]},
    purchase: { label:'Purchase', icon:'🛒', color:'#9b5de5', actions:[
        { label:'New Supplier', icon:'🏭', btnLabel:'Create Supplier',
          tool:'create_document', doctype:'Supplier',
          fields:[
            { n:'supplier_name',  l:'Supplier Name',  t:'text',   req:1, ph:'e.g. XYZ Supplies Ltd' },
            { n:'supplier_type',  l:'Supplier Type',  t:'select', req:0, opts:['Company','Individual'], def:'Company' },
            { n:'supplier_group', l:'Supplier Group', t:'link',   req:1, ldt:'Supplier Group', ph:'Search group...' },
            { n:'country',        l:'Country',        t:'link',   req:0, ldt:'Country', ph:'Search country...' },
        ]},
        { label:'Purchase Order', icon:'📦', btnLabel:'Create PO',
          tool:'create_document', doctype:'Purchase Order',
          fields:[
            { n:'supplier',      l:'Supplier',    t:'link', req:1, ldt:'Supplier', ph:'Search supplier...' },
            { n:'schedule_date', l:'Required By', t:'date', req:1, def:TODAY },
            { n:'_item_code', l:'Item Code', t:'link', req:1, ldt:'Item', ph:'Search item...', ct:'items', cf:'item_code' },
            { n:'_qty',       l:'Quantity',  t:'num',  req:1, def:'1',   ct:'items', cf:'qty' },
            { n:'_rate',      l:'Rate',      t:'num',  req:0, ph:'0.00', ct:'items', cf:'rate' },
        ]},
        { label:'Purchase Invoice', icon:'🧾', btnLabel:'Create Purchase Invoice',
          tool:'create_document', doctype:'Purchase Invoice',
          fields:[
            { n:'supplier',     l:'Supplier',     t:'link', req:1, ldt:'Supplier', ph:'Search supplier...' },
            { n:'posting_date', l:'Posting Date', t:'date', req:1, def:TODAY },
            { n:'_item_code', l:'Item Code', t:'link', req:1, ldt:'Item', ph:'Search item...', ct:'items', cf:'item_code' },
            { n:'_qty',       l:'Quantity',  t:'num',  req:1, def:'1',   ct:'items', cf:'qty' },
            { n:'_rate',      l:'Rate',      t:'num',  req:0, ph:'0.00', ct:'items', cf:'rate' },
        ]},
        { label:'List Suppliers', icon:'📋', prompt:'List all suppliers' },
        { label:'List PO',        icon:'📋', prompt:'List recent purchase orders' },
    ]},
    accounting: { label:'Accounting', icon:'💰', color:'#f77f00', actions:[
        { label:'Payment Entry', icon:'💳', btnLabel:'Create Payment',
          tool:'create_document', doctype:'Payment Entry',
          fields:[
            { n:'payment_type', l:'Payment Type', t:'select', req:1, opts:['Receive','Pay','Internal Transfer'], def:'Receive' },
            { n:'party_type',   l:'Party Type',   t:'select', req:1, opts:['Customer','Supplier','Employee'], def:'Customer' },
            { n:'party',        l:'Party Name',   t:'text',   req:1, ph:'Name' },
            { n:'paid_amount',  l:'Amount',       t:'num',    req:1, ph:'0.00' },
            { n:'posting_date', l:'Date',         t:'date',   req:1, def:TODAY },
        ]},
        { label:'Journal Entry', icon:'📒', btnLabel:'Create Journal Entry',
          tool:'create_document', doctype:'Journal Entry',
          fields:[
            { n:'voucher_type', l:'Type',    t:'select', req:1, opts:['Journal Entry','Bank Entry','Cash Entry','Credit Note','Debit Note'], def:'Journal Entry' },
            { n:'posting_date', l:'Date',    t:'date',   req:1, def:TODAY },
            { n:'user_remark',  l:'Remark', t:'text',   req:0, ph:'Optional narration...' },
        ]},
        { label:'List Payments', icon:'📋', prompt:'List recent payment entries' },
    ]},
    hr: { label:'HR', icon:'👤', color:'#06d6a0', actions:[
        { label:'New Employee', icon:'👤', btnLabel:'Create Employee',
          tool:'create_document', doctype:'Employee',
          fields:[
            { n:'first_name',      l:'First Name',      t:'text',   req:1, ph:'First name' },
            { n:'last_name',       l:'Last Name',       t:'text',   req:0, ph:'Last name' },
            { n:'gender',          l:'Gender',          t:'select', req:1, opts:['Male','Female','Other','Rather not say'], def:'Male' },
            { n:'date_of_joining', l:'Date of Joining', t:'date',   req:1, def:TODAY },
            { n:'department',      l:'Department',      t:'link',   req:1, ldt:'Department',  ph:'Search department...' },
            { n:'designation',     l:'Designation',     t:'link',   req:0, ldt:'Designation', ph:'Search designation...' },
        ]},
        { label:'Leave Request', icon:'🏖️', btnLabel:'Submit Leave',
          tool:'create_document', doctype:'Leave Application',
          fields:[
            { n:'employee',   l:'Employee',   t:'link', req:1, ldt:'Employee',   ph:'Search employee...' },
            { n:'leave_type', l:'Leave Type', t:'link', req:1, ldt:'Leave Type', ph:'Search...' },
            { n:'from_date',  l:'From Date',  t:'date', req:1, def:TODAY },
            { n:'to_date',    l:'To Date',    t:'date', req:1, def:TODAY },
            { n:'reason',     l:'Reason',     t:'text', req:0, ph:'Optional...' },
        ]},
        { label:'Expense Claim', icon:'🧾', btnLabel:'Create Expense Claim',
          tool:'create_document', doctype:'Expense Claim',
          fields:[
            { n:'employee',     l:'Employee', t:'link', req:1, ldt:'Employee', ph:'Search employee...' },
            { n:'posting_date', l:'Date',     t:'date', req:1, def:TODAY },
            { n:'remark',       l:'Remark',   t:'text', req:0, ph:'Optional...' },
        ]},
        { label:'List Employees', icon:'📋', prompt:'List all employees' },
    ]},
    inventory: { label:'Inventory', icon:'📦', color:'#3a86ff', actions:[
        { label:'New Item', icon:'📦', btnLabel:'Create Item',
          tool:'create_document', doctype:'Item',
          fields:[
            { n:'item_code',  l:'Item Code',  t:'text', req:1, ph:'e.g. ITEM-001' },
            { n:'item_name',  l:'Item Name',  t:'text', req:1, ph:'Display name' },
            { n:'item_group', l:'Item Group', t:'link', req:1, ldt:'Item Group', ph:'Search group...' },
            { n:'stock_uom',  l:'Stock UOM',  t:'link', req:1, ldt:'UOM', ph:'Nos, Kg...', def:'Nos' },
        ]},
        { label:'Stock Entry', icon:'📥', btnLabel:'Create Stock Entry',
          tool:'create_document', doctype:'Stock Entry',
          fields:[
            { n:'stock_entry_type', l:'Entry Type', t:'select', req:1, opts:['Material Receipt','Material Issue','Material Transfer','Material Consumption for Manufacture'], def:'Material Receipt' },
            { n:'_item_code',   l:'Item Code',       t:'link', req:1, ldt:'Item',      ph:'Search...', ct:'items', cf:'item_code' },
            { n:'_qty',         l:'Quantity',         t:'num',  req:1, def:'1',         ct:'items', cf:'qty' },
            { n:'_t_warehouse', l:'Target Warehouse', t:'link', req:0, ldt:'Warehouse', ph:'Search...', ct:'items', cf:'t_warehouse' },
        ]},
        { label:'List Items', icon:'📋', prompt:'List all items' },
    ]},
    crm: { label:'CRM', icon:'🎯', color:'#ef233c', actions:[
        { label:'New Lead', icon:'🎯', btnLabel:'Create Lead',
          tool:'create_document', doctype:'Lead',
          fields:[
            { n:'lead_name',    l:'Lead Name',   t:'text',   req:1, ph:'Full name' },
            { n:'company_name', l:'Company',     t:'text',   req:0, ph:'Company (optional)' },
            { n:'email_id',     l:'Email',       t:'email',  req:0, ph:'email@example.com' },
            { n:'mobile_no',    l:'Mobile',      t:'text',   req:0, ph:'+1234567890' },
            { n:'source',       l:'Lead Source', t:'select', req:0, opts:['','Cold Calling','Email','Existing Customer','LinkedIn','Reference','Social Media','Website'], def:'' },
        ]},
        { label:'New Opportunity', icon:'🎪', btnLabel:'Create Opportunity',
          tool:'create_document', doctype:'Opportunity',
          fields:[
            { n:'opportunity_from', l:'From',         t:'select', req:1, opts:['Customer','Lead'], def:'Lead' },
            { n:'party_name',       l:'Customer/Lead', t:'text',   req:1, ph:'Name' },
            { n:'opportunity_type', l:'Opp. Type',    t:'link',   req:0, ldt:'Opportunity Type', ph:'Search...' },
            { n:'transaction_date', l:'Date',          t:'date',   req:1, def:TODAY },
        ]},
        { label:'List Leads',        icon:'📋', prompt:'List all leads' },
        { label:'List Opportunities',icon:'📋', prompt:'List all opportunities' },
    ]},
};

// ── CSS ─────────────────────────────────────────────────
const CSS = `
/* FAB */
#nai-fab {
    position:fixed; bottom:28px; right:28px;
    width:54px; height:54px; border-radius:50%;
    background:linear-gradient(135deg,#00f0ff,#a000ff);
    border:none; cursor:pointer; z-index:100000;
    box-shadow:0 4px 20px rgba(160,0,255,0.5);
    display:flex; align-items:center; justify-content:center;
    transition:transform 0.2s,box-shadow 0.2s;
}
#nai-fab:hover{transform:scale(1.1);box-shadow:0 6px 28px rgba(160,0,255,0.65)}
#nai-fab svg{color:#fff;pointer-events:none}

/* Panel */
#nai-panel{
    position:fixed; bottom:94px; right:28px;
    width:400px; height:640px;
    background:#0d0d1f;
    border:1px solid rgba(255,255,255,0.07);
    border-radius:20px;
    box-shadow:0 20px 60px rgba(0,0,0,0.7),0 0 0 1px rgba(0,240,255,0.05);
    z-index:99999; display:flex; flex-direction:column;
    overflow:hidden; position:fixed;
    transform:scale(0.9) translateY(20px); opacity:0; pointer-events:none;
    transition:transform 0.25s cubic-bezier(.34,1.56,.64,1),opacity 0.2s ease;
}
#nai-panel.nai-open{transform:scale(1) translateY(0);opacity:1;pointer-events:all}

/* Header */
.nai-ph{
    display:flex; align-items:center; gap:10px; padding:13px 14px;
    background:linear-gradient(135deg,rgba(0,20,40,0.95),rgba(20,0,40,0.95));
    border-bottom:1px solid rgba(255,255,255,0.06); flex-shrink:0;
    position:relative;
}
.nai-ph-logo{
    width:32px; height:32px; border-radius:50%;
    background:linear-gradient(135deg,#00f0ff,#a000ff);
    display:flex; align-items:center; justify-content:center; flex-shrink:0;
    box-shadow:0 0 12px rgba(0,240,255,0.4);
}
.nai-ph-logo svg{color:#fff}
.nai-ph-info{flex:1; min-width:0}
.nai-ph-title{
    font-size:0.82rem; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    background:linear-gradient(135deg,#00f0ff,#c87aff);
    -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text;
}
.nai-ph-status{display:flex; align-items:center; gap:5px; margin-top:1px}
.nai-ph-dot{
    width:6px; height:6px; border-radius:50%; background:#00ff88;
    box-shadow:0 0 6px #00ff88; animation:nai-pulse 2s infinite;
}
@keyframes nai-pulse{0%,100%{opacity:1}50%{opacity:0.5}}
.nai-ph-online{font-size:0.62rem; color:#00ff88; -webkit-text-fill-color:#00ff88}
.nai-ph-actions{display:flex; gap:3px}
.nai-ph-btn{
    background:transparent; border:none; color:rgba(255,255,255,0.4); cursor:pointer;
    border-radius:8px; width:28px; height:28px;
    display:flex; align-items:center; justify-content:center;
    transition:background 0.15s,color 0.15s;
}
.nai-ph-btn:hover{background:rgba(255,255,255,0.08);color:#fff}

/* Messages */
#nai-msgs{
    flex:1; overflow-y:auto; padding:14px 12px;
    display:flex; flex-direction:column; gap:10px;
    scrollbar-width:thin; scrollbar-color:rgba(255,255,255,0.1) transparent;
}
#nai-msgs::-webkit-scrollbar{width:3px}
#nai-msgs::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.1);border-radius:3px}

.nai-m{display:flex; gap:8px; animation:nai-up 0.22s ease}
@keyframes nai-up{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.nai-m.user{flex-direction:row-reverse}
.nai-av{
    width:26px; height:26px; border-radius:50%;
    font-size:0.6rem; font-weight:700;
    display:flex; align-items:center; justify-content:center;
    flex-shrink:0; align-self:flex-end; color:#fff;
}
.nai-m.user .nai-av{background:linear-gradient(135deg,#00f0ff,#a000ff)}
.nai-m.assistant .nai-av{background:rgba(255,255,255,0.1);color:rgba(255,255,255,0.6)}
.nai-bub{
    max-width:80%; padding:9px 13px; border-radius:14px;
    font-size:0.8rem; line-height:1.6; color:#e8e8f0; word-break:break-word;
}
.nai-m.user .nai-bub{
    background:rgba(0,180,216,0.15); border:1px solid rgba(0,180,216,0.25);
    border-bottom-right-radius:4px;
}
.nai-m.assistant .nai-bub{
    background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.08);
    border-bottom-left-radius:4px;
}
.nai-bub p{margin:0 0 5px}.nai-bub p:last-child{margin-bottom:0}
.nai-bub ul,.nai-bub ol{padding-left:16px;margin:3px 0}
.nai-bub code{background:rgba(255,255,255,0.1);border-radius:4px;padding:1px 5px;font-size:0.78em}
.nai-bub pre{background:rgba(0,0,0,0.4);border-radius:8px;padding:8px 12px;overflow-x:auto;margin:5px 0}
.nai-bub pre code{background:transparent;padding:0}
.nai-bub strong{color:#c8c8ff}

/* Typing */
.nai-typing .nai-bub{display:flex;align-items:center;gap:5px;padding:12px 16px}
.nai-dot{width:6px;height:6px;border-radius:50%;background:rgba(255,255,255,0.35);animation:nai-bounce 1.2s infinite}
.nai-dot:nth-child(2){animation-delay:.18s}.nai-dot:nth-child(3){animation-delay:.36s}
@keyframes nai-bounce{0%,80%,100%{transform:translateY(0);opacity:.35}40%{transform:translateY(-5px);opacity:1}}

/* Welcome screen */
.nai-welcome{
    flex:1; display:flex; flex-direction:column; align-items:center;
    justify-content:center; text-align:center; padding:20px 24px;
    gap:8px;
}
.nai-w-glow{
    width:60px; height:60px; border-radius:50%;
    background:linear-gradient(135deg,#00f0ff,#a000ff);
    display:flex; align-items:center; justify-content:center;
    box-shadow:0 0 30px rgba(0,240,255,0.3),0 0 60px rgba(160,0,255,0.2);
    margin-bottom:8px;
}
.nai-w-title{color:#fff;font-size:1rem;font-weight:700;margin:0}
.nai-w-sub{color:rgba(255,255,255,0.45);font-size:0.76rem;margin:0;line-height:1.5}

/* Quick-action bar */
#nai-qbar{
    flex-shrink:0; background:rgba(255,255,255,0.025);
    border-top:1px solid rgba(255,255,255,0.06); padding:8px 10px 0;
}
.nai-qbar-lbl{
    font-size:0.6rem; font-weight:700; color:rgba(255,255,255,0.3);
    text-transform:uppercase; letter-spacing:0.08em; margin-bottom:6px;
}
.nai-qcats{
    display:flex; gap:5px; flex-wrap:nowrap; overflow-x:auto;
    scrollbar-width:none; padding-bottom:7px;
}
.nai-qcats::-webkit-scrollbar{display:none}
.nai-qcat{
    display:flex; align-items:center; gap:5px;
    background:rgba(255,255,255,0.05);
    border:1px solid rgba(255,255,255,0.1);
    color:rgba(255,255,255,0.65); border-radius:20px;
    padding:5px 11px; font-size:0.71rem; cursor:pointer;
    white-space:nowrap; flex-shrink:0; transition:all 0.15s;
}
.nai-qcat:hover{background:rgba(255,255,255,0.1);color:#fff;border-color:rgba(255,255,255,0.2)}
.nai-qcat.active{color:#fff; border-color:var(--qcat-color,#00f0ff); background:rgba(0,240,255,0.1)}
.nai-qsubs{
    display:flex; gap:5px; flex-wrap:wrap;
    padding:0; max-height:0; overflow:hidden;
    transition:max-height 0.22s ease, padding 0.15s;
}
.nai-qsubs.open{max-height:110px; padding:6px 0 8px}
.nai-qsub{
    background:rgba(255,255,255,0.04); border:1px solid rgba(255,255,255,0.1);
    color:rgba(200,200,255,0.8); border-radius:8px; padding:4px 10px;
    font-size:0.71rem; cursor:pointer; white-space:nowrap;
    transition:all 0.15s;
}
.nai-qsub:hover{border-color:var(--qcat-color,#00f0ff);color:#fff;background:rgba(0,240,255,0.08)}

/* Input area */
.nai-inp-area{
    flex-shrink:0; padding:8px 10px 12px;
    border-top:1px solid rgba(255,255,255,0.06);
    background:rgba(255,255,255,0.02);
}
.nai-inp-row{display:flex;gap:8px;align-items:flex-end}
#nai-input-field{
    flex:1; resize:none; background:rgba(255,255,255,0.05);
    border:1px solid rgba(255,255,255,0.12);
    border-radius:12px; color:#e8e8f0; font-size:0.8rem; padding:9px 13px;
    outline:none; line-height:1.5; max-height:90px; overflow-y:auto;
    transition:border-color 0.2s,box-shadow 0.2s; scrollbar-width:thin; font-family:inherit;
}
#nai-input-field::placeholder{color:rgba(255,255,255,0.3)}
#nai-input-field:focus{border-color:rgba(160,0,255,0.6);box-shadow:0 0 0 3px rgba(160,0,255,0.1)}
#nai-send-btn{
    width:36px; height:36px; border-radius:50%;
    background:linear-gradient(135deg,#00f0ff,#a000ff);
    border:none; cursor:pointer; display:flex; align-items:center; justify-content:center;
    flex-shrink:0; transition:transform 0.15s,opacity 0.15s;
    box-shadow:0 2px 10px rgba(160,0,255,0.4);
}
#nai-send-btn:hover{transform:scale(1.1)}
#nai-send-btn:disabled{opacity:0.35;cursor:not-allowed;transform:none}
#nai-send-btn svg{color:#fff}

/* Action result card */
.nai-acard{
    background:linear-gradient(135deg,rgba(0,240,255,0.07),rgba(160,0,255,0.07));
    border:1px solid rgba(0,240,255,0.2); border-radius:12px;
    padding:10px 13px; margin-top:6px; font-size:0.77rem;
    color:#c8c8ff; display:flex; align-items:center; gap:10px;
    animation:nai-up 0.22s ease;
}
.nai-acard-ico{
    width:30px; height:30px; border-radius:50%;
    background:linear-gradient(135deg,#00f0ff,#a000ff);
    display:flex; align-items:center; justify-content:center;
    flex-shrink:0; color:#fff; font-size:0.8rem; font-weight:700;
    box-shadow:0 0 10px rgba(0,240,255,0.3);
}
.nai-acard a{color:#00f0ff;text-decoration:none;font-weight:600}
.nai-acard a:hover{text-decoration:underline}

/* ── Task Form Overlay ─────────────────────────────── */
#nai-task-form{
    position:absolute; bottom:0; left:0; right:0;
    background:#0b0b1c;
    border-top:1px solid rgba(255,255,255,0.08);
    border-radius:0 0 20px 20px;
    max-height:92%;
    overflow-y:auto;
    transform:translateY(100%);
    transition:transform 0.28s cubic-bezier(.34,1.15,.64,1);
    z-index:20;
    scrollbar-width:thin; scrollbar-color:rgba(255,255,255,0.1) transparent;
}
#nai-task-form.open{transform:translateY(0)}
#nai-task-form::-webkit-scrollbar{width:3px}
#nai-task-form::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.1);border-radius:3px}

.nai-tf-hdr{
    display:flex; align-items:center; gap:10px;
    padding:14px 16px 10px;
    background:#0b0b1c;
    border-bottom:1px solid rgba(255,255,255,0.06);
    position:sticky; top:0; z-index:5;
}
.nai-tf-icon{
    width:32px; height:32px; border-radius:10px;
    background:linear-gradient(135deg,var(--tf-color-a,#00f0ff),var(--tf-color-b,#a000ff));
    display:flex; align-items:center; justify-content:center; flex-shrink:0;
    font-size:0.95rem;
}
.nai-tf-title{flex:1; font-size:0.87rem; font-weight:700; color:#fff}
.nai-tf-sub{font-size:0.65rem; color:rgba(255,255,255,0.4); display:block; margin-top:1px}
.nai-tf-xbtn{
    background:transparent; border:none; color:rgba(255,255,255,0.4);
    cursor:pointer; border-radius:8px; width:28px; height:28px;
    display:flex; align-items:center; justify-content:center; font-size:1.1rem;
    transition:background 0.15s,color 0.15s;
}
.nai-tf-xbtn:hover{background:rgba(255,255,255,0.08);color:#fff}

.nai-tf-body{padding:14px 16px; display:flex; flex-direction:column; gap:13px}

.nai-tf-sep{
    font-size:0.62rem; font-weight:700; color:rgba(255,255,255,0.25);
    text-transform:uppercase; letter-spacing:0.07em;
    display:flex; align-items:center; gap:8px; margin:2px 0;
}
.nai-tf-sep::after{content:''; flex:1; height:1px; background:rgba(255,255,255,0.07)}

.nai-ff{display:flex; flex-direction:column; gap:5px}
.nai-fl{
    font-size:0.68rem; font-weight:600; color:rgba(255,255,255,0.5);
    text-transform:uppercase; letter-spacing:0.05em; display:flex; align-items:center; gap:4px;
}
.nai-fl .req{color:#ff6b6b}
.nai-fi, .nai-fs{
    background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.1);
    border-radius:9px; color:#e8e8f0; font-size:0.8rem; padding:8px 12px;
    outline:none; width:100%; box-sizing:border-box; font-family:inherit;
    transition:border-color 0.2s,box-shadow 0.2s;
}
.nai-fi:focus,.nai-fs:focus{
    border-color:rgba(160,0,255,0.6);
    box-shadow:0 0 0 3px rgba(160,0,255,0.1);
}
.nai-fi::placeholder{color:rgba(255,255,255,0.25)}
.nai-fs{
    appearance:none; cursor:pointer;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='rgba(255,255,255,0.35)'/%3E%3C/svg%3E");
    background-repeat:no-repeat; background-position:right 12px center; padding-right:30px;
}
.nai-fs option{background:#1a1a2e;color:#e8e8f0}

/* Link autocomplete */
.nai-lw{position:relative}
.nai-ldd{
    position:absolute; top:calc(100% + 3px); left:0; right:0;
    background:#161630; border:1px solid rgba(255,255,255,0.12);
    border-radius:9px; max-height:150px; overflow-y:auto;
    z-index:100; display:none; scrollbar-width:thin;
    box-shadow:0 8px 24px rgba(0,0,0,0.5);
    scrollbar-color:rgba(255,255,255,0.1) transparent;
}
.nai-ldd.open{display:block}
.nai-ldi{
    padding:8px 12px; font-size:0.78rem; color:rgba(200,200,255,0.85);
    cursor:pointer; transition:background 0.1s;
}
.nai-ldi:hover,.nai-ldi.hi{background:rgba(160,0,255,0.2);color:#fff}

/* Form footer */
.nai-tf-foot{
    display:flex; gap:8px; padding:10px 16px 18px;
    background:#0b0b1c;
    position:sticky; bottom:0; z-index:5;
    border-top:1px solid rgba(255,255,255,0.06);
}
.nai-tf-cancel{
    flex:1; border-radius:10px; padding:9px; font-size:0.8rem; font-weight:600;
    cursor:pointer; border:1px solid rgba(255,255,255,0.12);
    background:rgba(255,255,255,0.04); color:rgba(255,255,255,0.5);
    transition:all 0.15s;
}
.nai-tf-cancel:hover{background:rgba(255,255,255,0.08);color:#fff}
.nai-tf-submit{
    flex:2; border-radius:10px; padding:9px; font-size:0.8rem; font-weight:700;
    cursor:pointer; border:none;
    background:linear-gradient(135deg,#00c8ff,#9000e0);
    color:#fff; transition:opacity 0.15s,transform 0.15s;
    box-shadow:0 2px 12px rgba(160,0,255,0.4);
}
.nai-tf-submit:hover{opacity:0.9;transform:translateY(-1px)}
.nai-tf-submit:disabled{opacity:0.35;cursor:not-allowed;transform:none}
.nai-tf-err{
    font-size:0.72rem; color:#ff6b6b; padding:0 16px 10px;
    display:none;
}
.nai-tf-err.show{display:block}
`;

// ── DOM ──────────────────────────────────────────────
const STAR = `<svg xmlns="http://www.w3.org/2000/svg" width="15" height="13" fill="currentColor" viewBox="0 0 16 16"><path d="M7.657 6.247c.11-.33.576-.33.686 0l.645 1.937a2.89 2.89 0 0 0 1.829 1.828l1.936.645c.33.11.33.576 0 .686l-1.937.645a2.89 2.89 0 0 0-1.828 1.829l-.645 1.936a.361.361 0 0 1-.686 0l-.645-1.937a2.89 2.89 0 0 0-1.828-1.828l-1.937-.645a.361.361 0 0 1 0-.686l1.937-.645a2.89 2.89 0 0 0 1.828-1.828zM3.794 1.148a.217.217 0 0 1 .412 0l.387 1.162c.173.518.579.924 1.097 1.097l1.162.387a.217.217 0 0 1 0 .412l-1.162.387A1.73 1.73 0 0 0 4.593 5.69l-.387 1.162a.217.217 0 0 1-.412 0L3.407 5.69A1.73 1.73 0 0 0 2.31 4.593l-1.162-.387a.217.217 0 0 1 0-.412l1.162-.387A1.73 1.73 0 0 0 3.407 2.31z"/></svg>`;

function buildWidget() {
    if (document.getElementById('nai-fab')) return;

    const st = document.createElement('style');
    st.textContent = CSS;
    document.head.appendChild(st);

    const fab = document.createElement('button');
    fab.id = 'nai-fab';
    fab.title = 'Click 2 Click Consultant AI';
    fab.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="20" fill="currentColor" viewBox="0 0 16 16"><path d="M7.657 6.247c.11-.33.576-.33.686 0l.645 1.937a2.89 2.89 0 0 0 1.829 1.828l1.936.645c.33.11.33.576 0 .686l-1.937.645a2.89 2.89 0 0 0-1.828 1.829l-.645 1.936a.361.361 0 0 1-.686 0l-.645-1.937a2.89 2.89 0 0 0-1.828-1.828l-1.937-.645a.361.361 0 0 1 0-.686l1.937-.645a2.89 2.89 0 0 0 1.828-1.828zM3.794 1.148a.217.217 0 0 1 .412 0l.387 1.162c.173.518.579.924 1.097 1.097l1.162.387a.217.217 0 0 1 0 .412l-1.162.387A1.73 1.73 0 0 0 4.593 5.69l-.387 1.162a.217.217 0 0 1-.412 0L3.407 5.69A1.73 1.73 0 0 0 2.31 4.593l-1.162-.387a.217.217 0 0 1 0-.412l1.162-.387A1.73 1.73 0 0 0 3.407 2.31zM10.863.099a.145.145 0 0 1 .274 0l.258.774c.115.346.386.617.732.732l.774.258a.145.145 0 0 1 0 .274l-.774.258a1.16 1.16 0 0 0-.732.732l-.258.774a.145.145 0 0 1-.274 0l-.258-.774a1.16 1.16 0 0 0-.732-.732L9.1 2.137a.145.145 0 0 1 0-.274l.774-.258c.346-.115.617-.386.732-.732z"/></svg>`;

    const panel = document.createElement('div');
    panel.id = 'nai-panel';
    panel.innerHTML = `
        <div class="nai-ph">
            <div class="nai-ph-logo">${STAR}</div>
            <div class="nai-ph-info">
                <div class="nai-ph-title">Click 2 Click Consultant AI</div>
                <div class="nai-ph-status">
                    <div class="nai-ph-dot"></div>
                    <span class="nai-ph-online">Online</span>
                </div>
            </div>
            <div class="nai-ph-actions">
                <button class="nai-ph-btn" id="nai-clear" title="Clear chat">
                    <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" fill="currentColor" viewBox="0 0 16 16"><path d="M2.5 1a1 1 0 0 0-1 1v1a1 1 0 0 0 1 1H3v9a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2V4h.5a1 1 0 0 0 1-1V2a1 1 0 0 0-1-1H10a1 1 0 0 0-1-1H7a1 1 0 0 0-1 1zm3 4a.5.5 0 0 1 1 0v7a.5.5 0 0 1-1 0zm3 0a.5.5 0 0 1 1 0v7a.5.5 0 0 1-1 0z"/></svg>
                </button>
                <button class="nai-ph-btn" id="nai-close" title="Close">
                    <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" fill="currentColor" viewBox="0 0 16 16"><path d="M4.646 4.646a.5.5 0 0 1 .708 0L8 7.293l2.646-2.647a.5.5 0 0 1 .708.708L8.707 8l2.647 2.646a.5.5 0 0 1-.708.708L8 8.707l-2.646 2.647a.5.5 0 0 1-.708-.708L7.293 8 4.646 5.354a.5.5 0 0 1 0-.708z"/></svg>
                </button>
            </div>
        </div>

        <div id="nai-msgs">
            <div class="nai-welcome" id="nai-welcome">
                <div class="nai-w-glow">
                    <svg xmlns="http://www.w3.org/2000/svg" width="24" height="22" fill="white" viewBox="0 0 16 16"><path d="M7.657 6.247c.11-.33.576-.33.686 0l.645 1.937a2.89 2.89 0 0 0 1.829 1.828l1.936.645c.33.11.33.576 0 .686l-1.937.645a2.89 2.89 0 0 0-1.828 1.829l-.645 1.936a.361.361 0 0 1-.686 0l-.645-1.937a2.89 2.89 0 0 0-1.828-1.828l-1.937-.645a.361.361 0 0 1 0-.686l1.937-.645a2.89 2.89 0 0 0 1.828-1.828zM3.794 1.148a.217.217 0 0 1 .412 0l.387 1.162c.173.518.579.924 1.097 1.097l1.162.387a.217.217 0 0 1 0 .412l-1.162.387A1.73 1.73 0 0 0 4.593 5.69l-.387 1.162a.217.217 0 0 1-.412 0L3.407 5.69A1.73 1.73 0 0 0 2.31 4.593l-1.162-.387a.217.217 0 0 1 0-.412l1.162-.387A1.73 1.73 0 0 0 3.407 2.31z"/></svg>
                </div>
                <p class="nai-w-title">Click 2 Click Consultant AI</p>
                <p class="nai-w-sub">Pick a quick action below or type anything<br>to manage your ERPNext business.</p>
            </div>
        </div>

        <div id="nai-qbar">
            <div class="nai-qbar-lbl">Quick Actions</div>
            <div class="nai-qcats" id="nai-qcats"></div>
            <div class="nai-qsubs" id="nai-qsubs"></div>
        </div>

        <div class="nai-inp-area">
            <div class="nai-inp-row">
                <textarea id="nai-input-field" rows="1" placeholder="Ask anything about your business…"></textarea>
                <button id="nai-send-btn" title="Send">
                    <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" fill="currentColor" viewBox="0 0 16 16"><path d="M15.964.686a.5.5 0 0 0-.65-.65L.767 5.855H.766l-.452.18a.5.5 0 0 0-.082.887l.41.26.001.002 4.995 3.178 3.178 4.995.002.002.26.41a.5.5 0 0 0 .886-.083zm-1.833 1.89L6.637 10.07l-.215-.338a.5.5 0 0 0-.154-.154l-.338-.215 7.494-7.494 1.178-.471z"/></svg>
                </button>
            </div>
        </div>

        <div id="nai-task-form"></div>`;

    document.body.appendChild(fab);
    document.body.appendChild(panel);
    bindEvents(fab, panel);
}

// ── Logic ────────────────────────────────────────────
function bindEvents(fab, panel) {
    let open = false, busy = false, history = [], activeCat = null;

    const msgsEl   = panel.querySelector('#nai-msgs');
    const inputEl  = panel.querySelector('#nai-input-field');
    const sendBtn  = panel.querySelector('#nai-send-btn');
    const clearBtn = panel.querySelector('#nai-clear');
    const closeBtn = panel.querySelector('#nai-close');
    const welcomeEl= panel.querySelector('#nai-welcome');
    const qcatsEl  = panel.querySelector('#nai-qcats');
    const qsubsEl  = panel.querySelector('#nai-qsubs');
    const tfEl     = panel.querySelector('#nai-task-form');

    // ── Category tabs ────────────────────────────────
    Object.entries(QA).forEach(([key, cat]) => {
        const btn = document.createElement('button');
        btn.className = 'nai-qcat';
        btn.dataset.cat = key;
        btn.style.setProperty('--qcat-color', cat.color);
        btn.innerHTML = `${cat.icon} ${cat.label}`;
        btn.addEventListener('click', () => toggleCategory(key, btn, cat));
        qcatsEl.appendChild(btn);
    });

    function toggleCategory(key, btn, cat) {
        const same = activeCat === key;
        panel.querySelectorAll('.nai-qcat').forEach(b => b.classList.remove('active'));
        qsubsEl.innerHTML = '';
        qsubsEl.classList.remove('open');
        qsubsEl.style.removeProperty('--qcat-color');
        if (same) { activeCat = null; return; }

        activeCat = key;
        btn.classList.add('active');
        qsubsEl.style.setProperty('--qcat-color', cat.color);

        cat.actions.forEach(action => {
            const sub = document.createElement('button');
            sub.className = 'nai-qsub';
            sub.textContent = (action.icon ? action.icon + ' ' : '') + action.label;
            sub.addEventListener('click', () => {
                qsubsEl.classList.remove('open');
                btn.classList.remove('active');
                activeCat = null;
                if (action.fields) {
                    openTaskForm(action, cat);
                } else {
                    inputEl.value = action.prompt;
                    send();
                }
            });
            qsubsEl.appendChild(sub);
        });
        setTimeout(() => qsubsEl.classList.add('open'), 10);
    }

    // ── Task form ────────────────────────────────────
    function openTaskForm(action, cat) {
        tfEl.innerHTML = '';

        const hdr = document.createElement('div');
        hdr.className = 'nai-tf-hdr';
        hdr.innerHTML = `
            <div class="nai-tf-icon" style="--tf-color-a:${cat.color};--tf-color-b:${cat.color}aa">${action.icon || '📋'}</div>
            <div class="nai-tf-title">
                ${action.label}
                <span class="nai-tf-sub">${action.doctype} · Required fields marked *</span>
            </div>
            <button class="nai-tf-xbtn" id="nai-tf-x">✕</button>`;
        tfEl.appendChild(hdr);

        const body = document.createElement('div');
        body.className = 'nai-tf-body';

        let hasChildTable = action.fields.some(f => f.ct);
        let childSepAdded = false;

        action.fields.forEach(f => {
            if (f.ct && !childSepAdded) {
                const sep = document.createElement('div');
                sep.className = 'nai-tf-sep';
                sep.textContent = 'Item Details';
                body.appendChild(sep);
                childSepAdded = true;
            }
            body.appendChild(buildField(f, cat.color));
        });
        tfEl.appendChild(body);

        const errEl = document.createElement('div');
        errEl.className = 'nai-tf-err';
        errEl.id = 'nai-tf-err';
        tfEl.appendChild(errEl);

        const foot = document.createElement('div');
        foot.className = 'nai-tf-foot';
        foot.innerHTML = `
            <button class="nai-tf-cancel" id="nai-tf-cancel">Cancel</button>
            <button class="nai-tf-submit" id="nai-tf-submit">${action.btnLabel || 'Create'} →</button>`;
        tfEl.appendChild(foot);

        tfEl.querySelector('#nai-tf-x').addEventListener('click', closeTaskForm);
        tfEl.querySelector('#nai-tf-cancel').addEventListener('click', closeTaskForm);
        tfEl.querySelector('#nai-tf-submit').addEventListener('click', () => submitForm(action));

        setTimeout(() => tfEl.classList.add('open'), 10);

        const first = tfEl.querySelector('.nai-fi, .nai-fs');
        if (first) setTimeout(() => first.focus(), 300);
    }

    function closeTaskForm() {
        tfEl.classList.remove('open');
        setTimeout(() => { tfEl.innerHTML = ''; }, 280);
    }

    function buildField(f, accentColor) {
        const wrap = document.createElement('div');
        wrap.className = 'nai-ff';

        const lbl = document.createElement('label');
        lbl.className = 'nai-fl';
        lbl.htmlFor = 'nai-f-' + f.n;
        lbl.innerHTML = f.l + (f.req ? '<span class="req">*</span>' : '');
        wrap.appendChild(lbl);

        if (f.t === 'select') {
            const sel = document.createElement('select');
            sel.className = 'nai-fs';
            sel.id = 'nai-f-' + f.n;
            sel.dataset.fn = f.n;
            sel.dataset.req = f.req ? '1' : '0';
            (f.opts || []).forEach(opt => {
                const o = document.createElement('option');
                o.value = opt; o.textContent = opt || '— Select —';
                if (opt === (f.def || '')) o.selected = true;
                sel.appendChild(o);
            });
            wrap.appendChild(sel);
        } else if (f.t === 'link') {
            wrap.appendChild(buildLinkField(f, accentColor));
        } else {
            const inp = document.createElement('input');
            inp.className = 'nai-fi';
            inp.id = 'nai-f-' + f.n;
            inp.dataset.fn = f.n;
            inp.dataset.req = f.req ? '1' : '0';
            inp.type = f.t === 'date' ? 'date' : f.t === 'num' ? 'number' : f.t === 'email' ? 'email' : 'text';
            if (f.t === 'num') { inp.min = '0'; inp.step = 'any'; }
            inp.placeholder = f.ph || '';
            if (f.def) inp.value = f.def;
            wrap.appendChild(inp);
        }
        return wrap;
    }

    function buildLinkField(f, accentColor) {
        const lw = document.createElement('div');
        lw.className = 'nai-lw';

        const vis = document.createElement('input');
        vis.className = 'nai-fi';
        vis.type = 'text';
        vis.placeholder = f.ph || 'Search ' + f.ldt + '...';
        vis.autocomplete = 'off';
        if (f.def) vis.value = f.def;

        const hid = document.createElement('input');
        hid.type = 'hidden';
        hid.id = 'nai-f-' + f.n;
        hid.dataset.fn = f.n;
        hid.dataset.req = f.req ? '1' : '0';
        if (f.def) hid.value = f.def;

        const dd = document.createElement('div');
        dd.className = 'nai-ldd';

        let timer = null, hiIdx = -1;

        vis.addEventListener('input', () => {
            hid.value = '';
            const q = vis.value.trim();
            clearTimeout(timer);
            dd.classList.remove('open');
            if (!q) return;
            timer = setTimeout(() => {
                frappe.call({
                    method: 'frappe.client.get_list',
                    args: {
                        doctype: f.ldt,
                        filters: [['name', 'like', '%' + q + '%']],
                        fields: ['name'],
                        limit_page_length: 8,
                        ignore_user_permissions: 1,
                    },
                    callback(r) {
                        dd.innerHTML = '';
                        hiIdx = -1;
                        const list = r.message || [];
                        if (!list.length) {
                            dd.innerHTML = '<div class="nai-ldi" style="color:rgba(255,255,255,0.3)">No results</div>';
                        } else {
                            list.forEach(rec => {
                                const item = document.createElement('div');
                                item.className = 'nai-ldi';
                                item.textContent = rec.name;
                                item.addEventListener('mousedown', e => {
                                    e.preventDefault();
                                    vis.value = rec.name;
                                    hid.value = rec.name;
                                    dd.classList.remove('open');
                                });
                                dd.appendChild(item);
                            });
                        }
                        dd.classList.add('open');
                    }
                });
            }, 260);
        });

        vis.addEventListener('keydown', e => {
            const items = dd.querySelectorAll('.nai-ldi');
            if (!items.length) return;
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                hiIdx = Math.min(hiIdx + 1, items.length - 1);
                items.forEach((el, i) => el.classList.toggle('hi', i === hiIdx));
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                hiIdx = Math.max(hiIdx - 1, 0);
                items.forEach((el, i) => el.classList.toggle('hi', i === hiIdx));
            } else if (e.key === 'Enter' && hiIdx >= 0) {
                e.preventDefault();
                items[hiIdx].dispatchEvent(new MouseEvent('mousedown'));
            } else if (e.key === 'Escape') {
                dd.classList.remove('open');
            }
        });

        vis.addEventListener('blur', () => {
            setTimeout(() => {
                dd.classList.remove('open');
                if (vis.value && !hid.value) hid.value = vis.value;
            }, 200);
        });

        lw.appendChild(vis);
        lw.appendChild(hid);
        lw.appendChild(dd);
        return lw;
    }

    function collectFormValues() {
        const vals = {};
        tfEl.querySelectorAll('[data-fn]').forEach(el => {
            vals[el.dataset.fn] = el.value;
        });
        return vals;
    }

    function buildArgs(action, vals) {
        const data = {};
        const childRows = {};

        action.fields.forEach(f => {
            const v = vals[f.n];
            if (v === undefined || v === null || v === '') return;
            const coerced = f.t === 'num' ? (parseFloat(v) || 0) : v;

            if (f.ct) {
                if (!childRows[f.ct]) childRows[f.ct] = [{}];
                childRows[f.ct][0][f.cf || f.n] = coerced;
            } else {
                data[f.n] = coerced;
            }
        });

        Object.assign(data, childRows);
        return { doctype: action.doctype, data };
    }

    function submitForm(action) {
        const errEl = tfEl.querySelector('#nai-tf-err');
        const submitBtn = tfEl.querySelector('#nai-tf-submit');

        // Validate required
        let missing = [];
        tfEl.querySelectorAll('[data-fn][data-req="1"]').forEach(el => {
            if (!el.value || !el.value.trim()) {
                const field = action.fields.find(f => f.n === el.dataset.fn);
                missing.push(field ? field.l : el.dataset.fn);
                el.style.borderColor = 'rgba(255,107,107,0.7)';
                el.addEventListener('input', () => { el.style.borderColor = ''; }, { once: true });
            }
        });

        if (missing.length) {
            errEl.textContent = 'Required: ' + missing.join(', ');
            errEl.classList.add('show');
            return;
        }
        errEl.classList.remove('show');

        const vals = collectFormValues();
        const args = buildArgs(action, vals);

        submitBtn.disabled = true;
        submitBtn.textContent = 'Creating…';

        frappe.call({
            method: 'next_ai.ai.execute_action',
            args: {
                tool_name: action.tool,
                args: JSON.stringify(args),
            },
            callback(r) {
                const res = r.message || {};
                closeTaskForm();
                if (res.success) {
                    addMsg('assistant', res.summary || `${action.doctype} created successfully.`);
                    addActionCard(res);
                } else {
                    addMsg('assistant', `Sorry, could not create ${action.doctype}: ${res.error || 'Unknown error'}`);
                }
                submitBtn.disabled = false;
            },
            error() {
                closeTaskForm();
                addMsg('assistant', 'An error occurred while creating the record.');
                submitBtn.disabled = false;
            }
        });
    }

    // ── Panel toggle ─────────────────────────────────
    function toggle() {
        open = !open;
        panel.classList.toggle('nai-open', open);
        if (open) inputEl.focus();
    }

    fab.addEventListener('click', toggle);
    closeBtn.addEventListener('click', () => { open = false; panel.classList.remove('nai-open'); });
    clearBtn.addEventListener('click', () => {
        history = [];
        msgsEl.innerHTML = '';
        msgsEl.appendChild(welcomeEl);
        inputEl.focus();
    });

    sendBtn.addEventListener('click', send);
    inputEl.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    inputEl.addEventListener('input', () => {
        inputEl.style.height = 'auto';
        inputEl.style.height = Math.min(inputEl.scrollHeight, 90) + 'px';
    });

    // ── Chat helpers ─────────────────────────────────
    function getInitials() {
        const n = frappe.session.user_fullname || frappe.session.user || 'U';
        return n.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase();
    }

    function escHtml(t) {
        return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    function renderMarkdown(text) {
        let h = escHtml(text);
        h = h.replace(/```[\w]*\n?([\s\S]*?)```/g,'<pre><code>$1</code></pre>');
        h = h.replace(/`([^`]+)`/g,'<code>$1</code>');
        h = h.replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>');
        h = h.replace(/\*(.*?)\*/g,'<em>$1</em>');
        h = h.replace(/^#{1,4} (.+)$/gm,'<strong>$1</strong>');
        h = h.replace(/^\s*[-*] (.+)$/gm,'<li>$1</li>');
        h = h.split(/\n{2,}/).map(b=>b.trim()).filter(Boolean)
              .map(b=>b.startsWith('<')?b:`<p>${b.replace(/\n/g,'<br>')}</p>`).join('');
        return h;
    }

    function addMsg(role, content, typing=false) {
        if (welcomeEl && welcomeEl.parentNode === msgsEl) msgsEl.removeChild(welcomeEl);
        const wrap = document.createElement('div');
        wrap.className = `nai-m ${role}`;
        const av = document.createElement('div');
        av.className = 'nai-av';
        av.textContent = role === 'user' ? getInitials() : 'AI';
        const bub = document.createElement('div');
        bub.className = 'nai-bub';
        if (typing) {
            wrap.classList.add('nai-typing');
            bub.innerHTML = '<div class="nai-dot"></div><div class="nai-dot"></div><div class="nai-dot"></div>';
        } else {
            bub.innerHTML = role === 'assistant'
                ? renderMarkdown(content)
                : `<p>${escHtml(content).replace(/\n/g,'<br>')}</p>`;
        }
        wrap.appendChild(av);
        wrap.appendChild(bub);
        msgsEl.appendChild(wrap);
        msgsEl.scrollTop = msgsEl.scrollHeight;
        return wrap;
    }

    function setSending(state) {
        busy = state;
        sendBtn.disabled = state;
        inputEl.disabled = state;
    }

    function addActionCard(action) {
        if (!action || !action.success) return;
        const card = document.createElement('div');
        card.className = 'nai-acard';
        const ico = (action.doctype || '?')[0].toUpperCase();
        const route = (action.url || '').replace('/app/','').replace(/\//g,',');
        card.innerHTML = `
            <div class="nai-acard-ico">${ico}</div>
            <div>
                <strong>${action.doctype}</strong>:
                <a href="${action.url}" onclick="frappe.set_route('${route}');return false;">${action.name}</a>
                <br><span style="color:rgba(255,255,255,0.3);font-size:0.7rem">Click to open in ERPNext</span>
            </div>`;
        msgsEl.appendChild(card);
        msgsEl.scrollTop = msgsEl.scrollHeight;
    }

    function send() {
        const text = inputEl.value.trim();
        if (!text || busy) return;
        addMsg('user', text);
        history.push({ role: 'user', content: text });
        inputEl.value = '';
        inputEl.style.height = 'auto';
        setSending(true);
        const typing = addMsg('assistant', '', true);

        frappe.call({
            method: 'next_ai.ai.chat_with_nextai',
            args: { message: text, history: JSON.stringify(history.slice(0,-1)) },
            callback(r) {
                msgsEl.removeChild(typing);
                const reply = r.message?.message || 'Sorry, something went wrong.';
                addMsg('assistant', reply);
                history.push({ role: 'assistant', content: reply });
                if (r.message?.action?.success) addActionCard(r.message.action);
                setSending(false);
                inputEl.focus();
            },
            error() {
                msgsEl.removeChild(typing);
                addMsg('assistant', 'An error occurred. Please try again.');
                setSending(false);
                inputEl.focus();
            }
        });
    }
}

// ── Boot ─────────────────────────────────────────────
function tryInitWidget(n) {
    n = n || 0;
    if (n > 30) return;
    if (typeof frappe==='undefined'||!frappe.session||!frappe.session.user||frappe.session.user==='Guest') {
        setTimeout(()=>tryInitWidget(n+1), 500);
        return;
    }
    buildWidget();
}

$(document).ready(function () { setTimeout(tryInitWidget, 1000); });
})();
