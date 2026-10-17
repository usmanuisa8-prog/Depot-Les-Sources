document.querySelectorAll(".add-to-cart").forEach(addToCart => {
    addToCart.addEventListener("click", (e) => {

        const articleId = e.currentTarget.dataset.productid

        modalOverlay.classList.remove("hidden")

        const quantityPopUp = document.querySelector(".quantity-pop-up")

        quantityPopUp.dataset.articleId = articleId
        quantityPopUp.classList.remove("hidden")
    })
})


document.querySelector(".quantity-pop-up .reset-quantity").addEventListener("click", (e) => {
    document.querySelector(".qpu-value").value = 1
    document.querySelector(".quantity-pop-up").classList.add("hidden")
    modalOverlay.classList.add("hidden")
})


document.querySelector(".quantity-pop-up .submit-quantity").addEventListener("click", (e) => {

    const articleId = e.target.closest(".quantity-pop-up").dataset.articleId
    const quantity = Number(document.querySelector(".quantity-pop-up .qpu-value").value)

    addToCart(articleId, quantity)

    document.querySelector(".qpu-value").value = 1
    document.querySelector(".quantity-pop-up").classList.add("hidden")
    modalOverlay.classList.add("hidden")
})


function addToCart(articleId, quantity) {

    const normalizedArticleId = Number(articleId)
    const normalizedQuantity = Number(quantity)

    if (!Number.isInteger(normalizedArticleId) || normalizedArticleId <= 0) {
        console.error("Invalid article ID:", articleId)
        return
    }

    if (!Number.isFinite(normalizedQuantity) || normalizedQuantity <= 0) {
        console.error("Invalid quantity:", quantity)
        return
    }

    let cart = JSON.parse(localStorage.getItem("cart")) || []

    // Remove any corrupted legacy entries.
    cart = cart.filter(item =>
        Number.isInteger(Number(item.articleId)) &&
        Number(item.articleId) > 0 &&
        Number.isFinite(Number(item.quantity)) &&
        Number(item.quantity) > 0
    )

    const index = cart.findIndex(
        item => Number(item.articleId) === normalizedArticleId
    )

    if (index !== -1) {
        cart[index].quantity =
            Number(cart[index].quantity) + normalizedQuantity
    } else {
        cart.push({
            articleId: normalizedArticleId,
            quantity: normalizedQuantity
        })
    }

    localStorage.setItem("cart", JSON.stringify(cart))

    console.log("Cart:", JSON.parse(localStorage.getItem("cart")))
}