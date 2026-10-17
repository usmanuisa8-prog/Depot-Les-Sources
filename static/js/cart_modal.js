const modalOverlay = document.querySelector(".modal-overlay")
const shoppingCart = document.querySelector(".shopping-cart-modal")
const container = shoppingCart.querySelector(".sc-content")
const shoppingCartBtn = document.querySelector(".shopping-cart")

shoppingCartBtn.addEventListener("click", () => {
   displayCart()
})

modalOverlay.addEventListener("click", (e) =>{
    if(e.target === modalOverlay){

        e.stopPropagation()
        document.querySelectorAll(".modal").forEach(modal =>{
            modal.classList.add("hidden")
        })
        modalOverlay.classList.add("hidden")
    }
})

document.querySelector(".modal-close").addEventListener("click", (e)=>{
    e.target.closest(".modal").classList.add("hidden")
    modalOverlay.classList.add("hidden")
})

async function displayCart(){
    const cart = JSON.parse(localStorage.getItem("cart")) || []
    console.log(cart)
    let html = ""

    if (cart.length > 0){
        const ids = cart.map(item => item.articleId)

        const quantities = {}

        cart.forEach(item => {
            quantities[String(item.articleId)] = Number(item.quantity)
        })

        const r = await fetch("/api/cart/details", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            body: JSON.stringify({
                article_ids: ids,
                quantities: quantities
            })
        })

        const articles = await r.json()

        let total = 0

        articles.forEach(article => {
            const cartItem = cart.find(
                item => Number(item.articleId) === Number(article.articleId)
            )

            if (!cartItem) {
                return
            }

            const quantity = Number(cartItem.quantity)

            const price = Number(
                article.cartUnitPrice !== undefined
                    ? article.cartUnitPrice
                    : (
                        quantity >= Number(article.articleBulkLimit)
                            ? article.articleBulkFinalPrice
                            : article.articleFinalPrice
                    )
            )

            const subtotal = Number(
                article.cartSubtotal !== undefined
                    ? article.cartSubtotal
                    : price * quantity
            )

            const promotionHtml =
                article.promotionName &&
                Number(article.promotionDiscount) > 0
                    ? `<p class="sc-d-promotion">
                            ${article.promotionName}
                       </p>`
                    : ""

            html += `<div class="sc-item">
                <div class="sc-item-image">
                    <img src="/static//images/products/${article.articleImage}" alt="${article.articleName}">
                </div>
                <div class="sc-item-description">
                    <p class="sc-item-error hidden">Stock insuffisant</p>
                    <h2 class="sc-d-name">${article.articleName} ${article.articleVolume}</h2>
                    <p class="sc-d-price">
                        <span>${price.toLocaleString("fr-FR")}</span> FCFA
                    </p>
                    ${promotionHtml}
                    <input type="number" name="item_quantity" class="sc-d-quantity" value="${quantity}">
                </div>
                <div class="sc-item-total">
                    <p>
                        Sous-total :
                        <span>${subtotal.toLocaleString("fr-FR")} FCFA</span>
                    </p>
                </div>
            </div>`
            total += subtotal
        })
        
        document.querySelector(".total-cart h2").textContent = `TOTAL : ${total} FCFA`
        document.querySelector(".sc-footer").classList.remove("hidden")
    } else {

        html += `<div class="sc-item">
                    <p>Votre cart est vide</p>
                    <a href="/products">
                        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 448 512"><!--! Font Awesome Free 7.1.0 by @fontawesome - https://fontawesome.com License - https://fontawesome.com/license/free (Icons: CC BY 4.0, Fonts: SIL OFL 1.1, Code: MIT License) Copyright 2025 Fonticons, Inc. --><path fill="currentColor" d="M256 64c0-17.7-14.3-32-32-32s-32 14.3-32 32l0 160-160 0c-17.7 0-32 14.3-32 32s14.3 32 32 32l160 0 0 160c0 17.7 14.3 32 32 32s32-14.3 32-32l0-160 160 0c17.7 0 32-14.3 32-32s-14.3-32-32-32l-160 0 0-160z"/></svg>
                        Ajouter
                    </a>
                </div>`
        document.querySelector(".sc-footer").classList.add("hidden")
    }

    container.innerHTML = html
    modalOverlay.classList.remove("hidden")
    document.querySelector(".shopping-cart-modal").classList.remove("hidden")
}

document.querySelector("#reset-cart").addEventListener("click", (e)=>{
    hideModal(document.querySelector(".shopping-cart-modal"))
    showModalDialog(
        "clearCart",
        "",
        `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 640" class="orange"><!--! Font Awesome Free 7.1.0 by @fontawesome - https://fontawesome.com License - https://fontawesome.com/license/free (Icons: CC BY 4.0, Fonts: SIL OFL 1.1, Code: MIT License) Copyright 2025 Fonticons, Inc. --><path fill="currentColor" d="M320 64C334.7 64 348.2 72.1 355.2 85L571.2 485C577.9 497.4 577.6 512.4 570.4 524.5C563.2 536.6 550.1 544 536 544L104 544C89.9 544 76.8 536.6 69.6 524.5C62.4 512.4 62.1 497.4 68.8 485L284.8 85C291.8 72.1 305.3 64 320 64zM320 416C302.3 416 288 430.3 288 448C288 465.7 302.3 480 320 480C337.7 480 352 465.7 352 448C352 430.3 337.7 416 320 416zM320 224C301.8 224 287.3 239.5 288.6 257.7L296 361.7C296.9 374.2 307.4 384 319.9 384C332.5 384 342.9 374.3 343.8 361.7L351.2 257.7C352.5 239.5 338.1 224 319.8 224z"/></svg>`,
        `<p>ÃƒÆ’Ã…Â tes-vous sÃƒÆ’Ã‚Â»r de vouloir vider votre panier ?`,
        true
    )
})

function clearCart(){
    localStorage.clear("cart")
    hideModal(document.querySelector(".shopping-cart-modal"))
    hideModal(document.querySelector(".dialog-modal"))
}

document.querySelector("#submit-cart").addEventListener("click", (e)=>{
    const cart = JSON.parse(localStorage.getItem("cart")) || []
   
    if (cart.length > 0){
        saveCommand(cart)
    }
})

async function saveCommand(cart) {

    try {

        // -----------------------------------------------------
        // Get available delivery zones
        // -----------------------------------------------------

        const zonesResponse = await fetch("/api/delivery-zones", {
            method: "GET",
            headers: {
                "Accept": "application/json"
            }
        })

        if (!zonesResponse.ok) {
            throw new Error("Unable to load delivery zones.")
        }

        const zones = await zonesResponse.json()

        if (!zones.length) {

            showModalDialog(
                "",
                "Error",
                "",
                `<p>No delivery zones are currently available.</p>`
            )

            return
        }

        // -----------------------------------------------------
        // Calculate products total
        // -----------------------------------------------------

        let productsTotal = 0

       const ids = cart.map(item => item.articleId)

const quantities = {}

cart.forEach(item => {
    quantities[String(item.articleId)] = Number(item.quantity)
})

const detailsResponse = await fetch("/api/cart/details", {
    method: "POST",
    headers: {
        "Content-Type": "application/json",
        "Accept": "application/json"
    },
    body: JSON.stringify({
        article_ids: ids,
        quantities: quantities
    })
})

        if (!detailsResponse.ok) {
            throw new Error("Unable to load cart details.")
        }

        const articles = await detailsResponse.json()

        cart.forEach(item => {

            const article = articles.find(
                a => Number(a.articleId) === Number(item.articleId)
            )

            if (!article) {
                throw new Error("A product in your cart could not be found.")
            }

            const quantity = Number(item.quantity)

            const subtotal = Number(
                article.cartSubtotal !== undefined
                    ? article.cartSubtotal
                    : (
                        quantity >= Number(article.articleBulkLimit)
                            ? Number(article.articleBulkFinalPrice)
                            : Number(article.articleFinalPrice)
                    ) * quantity
            )

            productsTotal += subtotal
        })

        // -----------------------------------------------------
        // Build checkout dialog
        // -----------------------------------------------------

        let zoneOptions = `
            <option value="">
                Select a delivery zone
            </option>
        `

        zones.forEach(zone => {

            zoneOptions += `
                <option
                    value="${zone.zoneId}"
                    data-fee="${zone.deliveryFee}"
                    data-minutes="${zone.estimatedMinutes}"
                >
                    ${zone.zoneName}
                    - ${Number(zone.deliveryFee).toLocaleString("fr-FR")} FCFA
                </option>
            `
        })

        const checkoutHtml = `
            <div class="checkout-form">

                <div class="checkout-field">

                    <label for="checkout-zone">
                        Delivery Zone
                    </label>

                    <select id="checkout-zone" required>
                        ${zoneOptions}
                    </select>

                </div>


                <div class="checkout-field">

                    <label for="checkout-address">
                        Delivery Address
                    </label>

                    <textarea
                        id="checkout-address"
                        rows="3"
                        maxlength="250"
                        placeholder="Enter your delivery address"
                        required
                    ></textarea>

                </div>


                <div class="checkout-field">

                    <label for="checkout-payment">
                        Payment Method
                    </label>

                    <select id="checkout-payment" required>

                        <option value="">
                            Select
                        </option>

                        <option value="MTN MoMo">
                            MTN Mobile Money
                        </option>

                        <option value="Orange Money">
                            Orange Money
                        </option>

                        <option value="Espèces">
                            Cash
                        </option>

                        <option value="CREDIT">
                            Purchase on Credit
                        </option>

                    </select>

                </div>


                <div class="checkout-summary">

                    <div>
                        <span>Products</span>
                        <strong id="checkout-products-total">
                            ${productsTotal.toLocaleString("fr-FR")} FCFA
                        </strong>
                    </div>

                    <div>
                        <span>Delivery</span>
                        <strong id="checkout-delivery-fee">
                            0 FCFA
                        </strong>
                    </div>

                    <div class="checkout-grand-total">
                        <span>Total</span>
                        <strong id="checkout-grand-total">
                            ${productsTotal.toLocaleString("fr-FR")} FCFA
                        </strong>
                    </div>

                    <div id="checkout-estimated-time"></div>

                </div>

            </div>
        `

        hideModal(document.querySelector(".shopping-cart-modal"))

        showModalDialog(
            "checkoutOrder",
            "Complete Your Order",
            "",
            checkoutHtml
        )

        // -----------------------------------------------------
        // Get dialog elements
        // -----------------------------------------------------

        const zoneSelect = document.querySelector("#checkout-zone")
        const addressInput = document.querySelector("#checkout-address")
        const paymentSelect = document.querySelector("#checkout-payment")

        const deliveryFeeElement =
            document.querySelector("#checkout-delivery-fee")

        const grandTotalElement =
            document.querySelector("#checkout-grand-total")

        const estimatedTimeElement =
            document.querySelector("#checkout-estimated-time")

        // -----------------------------------------------------
        // Update total when zone changes
        // -----------------------------------------------------

        zoneSelect.addEventListener("change", () => {

            const selectedOption =
                zoneSelect.options[zoneSelect.selectedIndex]

            if (!selectedOption || !selectedOption.value) {

                deliveryFeeElement.textContent = "0 FCFA"

                grandTotalElement.textContent =
                    `${productsTotal.toLocaleString("fr-FR")} FCFA`

                estimatedTimeElement.textContent = ""

                return
            }

            const deliveryFee =
                Number(selectedOption.dataset.fee || 0)

            const estimatedMinutes =
                Number(selectedOption.dataset.minutes || 0)

            const grandTotal =
                productsTotal + deliveryFee

            deliveryFeeElement.textContent =
                `${deliveryFee.toLocaleString("fr-FR")} FCFA`

            grandTotalElement.textContent =
                `${grandTotal.toLocaleString("fr-FR")} FCFA`

            estimatedTimeElement.textContent =
                `Estimated delivery: ${estimatedMinutes} minutes`
        })

        // -----------------------------------------------------
        // Add confirmation button
        // -----------------------------------------------------

        const dialogModal =
            document.querySelector(".dialog-modal")

        if (!dialogModal) {
            throw new Error("Checkout dialog could not be created.")
        }

        const dialogContent =
            dialogModal.querySelector(".dm-content")

        if (!dialogContent) {
            throw new Error("Checkout dialog content not found.")
        }

        const existingButton =
            dialogModal.querySelector("#confirm-checkout")

        if (existingButton) {
            existingButton.remove()
        }

        const confirmButton =
            document.createElement("button")

        confirmButton.id = "confirm-checkout"

        confirmButton.type = "button"

        confirmButton.textContent =
            "Confirm Order"

        confirmButton.className =
            "checkout-confirm-button"

        dialogContent.appendChild(confirmButton)

        // -----------------------------------------------------
        // Confirm order
        // -----------------------------------------------------

        confirmButton.addEventListener("click", async () => {

            const selectedZone = zoneSelect.value

            const deliveryAddress =
                addressInput.value.trim()

            const payementMethod =
                paymentSelect.value

            // ---------------------------------------------
            // Validation
            // ---------------------------------------------

            if (!selectedZone) {

                alert(
                    "Please select a delivery zone."
                )

                zoneSelect.focus()

                return
            }

            if (!deliveryAddress) {

                alert(
                    "Please enter your delivery address."
                )

                addressInput.focus()

                return
            }

            if (!payementMethod) {

                alert(
                    "Please select a payment method."
                )

                paymentSelect.focus()

                return
            }

            // ---------------------------------------------
            // Disable button
            // ---------------------------------------------

            confirmButton.disabled = true

            confirmButton.textContent =
                "Processing..."

            try {

                const response = await fetch(
                    "/api/cart/save_command",
                    {
                        method: "POST",

                        headers: {
                            "Content-Type": "application/json",
                            "Accept": "application/json"
                        },

                        body: JSON.stringify({

                            cart: cart,

                            deliveryAddress:
                                deliveryAddress,

                            paymentMethod:
                                payementMethod,

                            zoneId:
                                Number(selectedZone)
                        })
                    }
                )

                const result =
                    await response.json()

                if (!response.ok || !result.success) {

                    throw new Error(
                        result.error ||
                        result.message ||
                        "Error while saving the order."
                    )
                }

                // -----------------------------------------
                // Success
                // -----------------------------------------

                localStorage.removeItem("cart")

                hideModal(
                    document.querySelector(".dialog-modal")
                )

                document.querySelector(
                    ".sc-footer .total-cart h2"
                ).textContent = ""

                document.querySelector(
                    ".sc-footer"
                ).classList.add("hidden")

                showModalDialog(
                    "",
                    "Success",
                    "",
                    `
                        <p>
                            Order
                            <strong>${result.commandNumber}</strong>
                            was successfully placed.
                        </p>

                        <p>
                            Total:
                            <strong>
                                ${Number(result.grandTotal)
                                    .toLocaleString("fr-FR")}
                                FCFA
                            </strong>
                        </p>

                        <p>
                            Delivery:
                            <strong>
                                ${result.deliveryZone}
                            </strong>
                        </p>
                    `
                )

            } catch (error) {

                console.error(
                    "SAVE COMMAND ERROR:",
                    error
                )

                confirmButton.disabled = false

                confirmButton.textContent =
                    "Confirm Order"

                alert(
                    error.message ||
                    "Error while saving the order."
                )
            }

        })

    } catch (error) {

        console.error(
            "CHECKOUT ERROR:",
            error
        )

        showModalDialog(
            "",
            "Error",
            "",
            `
                <p>
                    Unable to prepare the order.
                </p>
            `
        )
    }
}
