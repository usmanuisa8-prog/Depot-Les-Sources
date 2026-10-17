function showModalDialog(action, title, icon, content, no=false){
    const modal = document.querySelector(".dialog-modal")
    modal.dataset.action = action
    showModal(modal)
    if (icon != ""){
        document.querySelector(".dm-icon-container").innerHTML = icon
    }
    document.querySelector(".dm-title").textContent = title
    document.querySelector(".dm-content").innerHTML = content
    if (no){
        document.querySelector(".dm-action.no").classList.remove("hidden")
    }
}

document.querySelector(".dm-action.ok").addEventListener("click", ()=>{
    const modal = document.querySelector(".dialog-modal")
    const action = modal.dataset.action
    switch(action){
        case "clearCart":
            clearCart()
            break;
        default:
            hideModal(modal)
    }
})

document.querySelector(".dm-action.no").addEventListener("click", ()=>{
    hideModal(document.querySelector(".dialog-modal"))
    showModal(document.querySelector(".shopping-cart-modal"))
})