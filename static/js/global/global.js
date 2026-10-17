function showModal(modal){
    document.querySelector(".modal-overlay").classList.remove("hidden")
    modal.classList.remove("hidden")
}

function hideModal(modal){
    document.querySelector(".modal-overlay").classList.add("hidden")
    modal.classList.add("hidden")
}