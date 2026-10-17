document.querySelector(".dm-action.ok").addEventListener("click", ()=>{
    const modal = document.querySelector(".dialog-modal")
    hideModal(modal)
}
)

function showModalDialog(title, icon, content){

    showModal(document.querySelector(".dialog-modal"))
    if (icon != ""){
        document.querySelector(".dm-icon-container").innerHTML = icon
    }
    document.querySelector(".dm-title").textContent = title
    document.querySelector(".dm-content").innerHTML = content
}